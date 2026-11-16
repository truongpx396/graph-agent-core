"""The session directory: which conversation threads exist and who owns them.

The web UI's switcher, the ownership gate on every message and the checks behind `GET /chat/sessions/{id}/messages` all read it. The rules
(a valid context is required, a title is cut to length, a bookkeeping write never fails a turn while an ownership check never defaults to
"yes") are the application's (`app/agent/runtime/sessions.py`); this is only where the rows live (`app/adapters/outbound/postgres/sessions.py`).

Every row is scoped by tenant, principal and domain, and every method takes all three: a thread opened under another tenant, person or
domain reads as not found. Methods raise when the store cannot be reached; the application decides what that means.
"""
from __future__ import annotations

import builtins
from datetime import datetime
from typing import Any, Protocol

from app.ports.slot import Slot


class SessionStore(Protocol):
    async def touch(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> None:
        """Records the thread, or refreshes its `last_active_at` when it exists. `title` and `domain` are written on first insert only."""
        ...

    async def claim(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> bool:
        """Atomically creates the thread if nobody has it, then reports whether this tenant, person and domain own it."""
        ...

    async def owns(self, tenant: str, principal: str, thread_id: str, domain: str) -> bool:
        """Whether the thread exists and belongs to exactly this tenant, person and domain. Does not create it."""
        ...

    async def list(self, tenant: str, principal: str, domain: str) -> list[dict[str, Any]]:
        """The person's threads in the domain, most recently active first: `thread_id`, `title`, `created_at`, `last_active_at`."""
        ...

    async def list_inactive(self, before: datetime, limit: int, after: tuple[datetime, str] | None = None) -> builtins.list[dict[str, Any]]:  # `list` is a method of this class: the builtin by name

        """Threads whose `last_active_at` is before `before`, oldest first, at most `limit`: `thread_id`, `tenant`, `principal`, `domain`, `last_active_at`.
        `after` is the `(last_active_at, thread_id)` of the last row of the previous page, so a run that deletes nothing (a dry run) still moves forward.

        The one method here that spans tenants and people, ON PURPOSE: retention is a property of the table, and the sweep that calls it is an operator's job
        (`scripts/jobs/checkpoint_sweep.py`). No request may reach it (`tests/core/test_session_sweep_is_not_reachable_from_requests.py`)."""
        ...

    async def sample_active(
        self, start: datetime, end: datetime, limit: int, tenants: tuple[str, ...] = ()
    ) -> builtins.list[dict[str, Any]]:  # `list` is a method of this class: the builtin by name
        """A random sample of at most `limit` threads whose `last_active_at` falls in `[start, end)`: `thread_id`, `tenant`, `principal`, `domain`, `last_active_at`. With
        `tenants`, only those tenants'. Spans tenants and people like `list_inactive`, ON PURPOSE and for the same reason: it is an operator's sampling job
        (`scripts/jobs/answer_sample.py`, PRODUCTION_REVIEW.md R28), and no request may reach it (`tests/core/test_session_sweep_is_not_reachable_from_requests.py`)."""
        ...

    async def delete(
        self, tenant: str, principal: str, thread_id: str, domain: str, *, inactive_before: datetime | None = None
    ) -> bool:
        """Removes the thread's row if it belongs to exactly this tenant, person and domain, and reports whether a row went. With `inactive_before`, only if it
        is STILL inactive since then: a thread that got a message while a sweep was running keeps its row. Idempotent."""
        ...


session_store: Slot[SessionStore] = Slot("session_store", SessionStore)
