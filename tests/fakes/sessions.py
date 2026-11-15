"""Fakes for `SessionStore` (`app/ports/outbound/sessions.py`) and `Checkpointing` (`app/ports/outbound/checkpointing.py`).

`tests/adapters/outbound/postgres/test_sessions_sweep_sql.py` pins the SQL the real store runs, and `tests/integration/test_checkpoint_retention_real_postgres.py`
the real tables; these say what the methods MEAN, for the tests of what is built on them."""
import builtins
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver


class InMemorySessionStore:
    """`rows` by thread id; a test sets `last_active_at` directly to age a thread. `log` is shared with `InMemoryCheckpointing` to see the order of calls."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.log = log if log is not None else []
        self.fail_delete: Exception | None = None

    def add(self, thread_id: str, *, tenant: str = "acme", principal: str = "alice", domain: str = "ecorp", last_active_at: datetime | None = None) -> None:
        now = datetime.now(UTC)
        self.rows[thread_id] = {
            "thread_id": thread_id, "tenant": tenant, "principal": principal, "domain": domain, "title": "t",
            "created_at": now, "last_active_at": last_active_at or now,
        }

    def _owned(self, tenant: str, principal: str, thread_id: str, domain: str) -> bool:
        row = self.rows.get(thread_id)
        return row is not None and (row["tenant"], row["principal"], row["domain"]) == (tenant, principal, domain)

    async def touch(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> None:
        if thread_id in self.rows:
            self.rows[thread_id]["last_active_at"] = datetime.now(UTC)
        else:
            self.add(thread_id, tenant=tenant, principal=principal, domain=domain)

    async def claim(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> bool:
        if thread_id not in self.rows:
            self.add(thread_id, tenant=tenant, principal=principal, domain=domain)
        return self._owned(tenant, principal, thread_id, domain)

    async def owns(self, tenant: str, principal: str, thread_id: str, domain: str) -> bool:
        return self._owned(tenant, principal, thread_id, domain)

    async def list(self, tenant: str, principal: str, domain: str) -> list[dict[str, Any]]:
        mine = [r for r in self.rows.values() if (r["tenant"], r["principal"], r["domain"]) == (tenant, principal, domain)]
        return sorted(mine, key=lambda r: r["last_active_at"], reverse=True)

    async def list_inactive(self, before: datetime, limit: int, after: tuple[datetime, str] | None = None) -> builtins.list[dict[str, Any]]:
        quiet = sorted((r for r in self.rows.values() if r["last_active_at"] < before), key=lambda r: (r["last_active_at"], r["thread_id"]))
        if after is not None:
            quiet = [r for r in quiet if (r["last_active_at"], r["thread_id"]) > after]
        return [dict(r) for r in quiet[:limit]]

    async def delete(self, tenant: str, principal: str, thread_id: str, domain: str, *, inactive_before: datetime | None = None) -> bool:
        self.log.append(f"row:{thread_id}")
        if self.fail_delete is not None:
            raise self.fail_delete
        if not self._owned(tenant, principal, thread_id, domain):
            return False
        if inactive_before is not None and not self.rows[thread_id]["last_active_at"] < inactive_before:
            return False
        del self.rows[thread_id]
        return True


class InMemoryCheckpointing:
    """Remembers which threads were deleted. `open` must run first, as the adapter's does. `on_delete` runs inside `delete_thread`, to script a conversation
    that wakes up while it is being deleted; `fail_for` makes a thread's delete raise."""

    def __init__(self, log: list[str] | None = None) -> None:
        self.log = log if log is not None else []
        self.opened = False
        self.closed = False
        self.deleted: list[str] = []
        self.fail_for: set[str] = set()
        self.on_delete: Callable[[str], None] | None = None

    async def open(self) -> BaseCheckpointSaver[Any]:
        self.opened, self.closed = True, False
        return MemorySaver()

    async def close(self) -> None:
        self.closed = True

    async def delete_thread(self, thread_id: str) -> None:
        if not self.opened:
            raise RuntimeError("delete_thread was called before open()")
        self.log.append(f"checkpoints:{thread_id}")
        if thread_id in self.fail_for:
            raise ConnectionError("the checkpointer is unreachable")
        self.deleted.append(thread_id)
        if self.on_delete is not None:
            self.on_delete(thread_id)
