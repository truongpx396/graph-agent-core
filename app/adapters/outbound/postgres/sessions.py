"""The `SessionStore` adapter (`app/ports/outbound/sessions.py`) over the appdata Postgres: the `chat_sessions` table
(postgres-init/06-chat-sessions.sql).

It is not the usage table (that one no-ops on a zero-token or rejected turn and carries no title, so a session with no billable tokens would
vanish from the switcher) and not the checkpointer's `checkpoints` table (no tenant or principal column to scope by, and a title parsed out of
its blob format would be fragile). Statements only: whether a write may fail a turn is the application's call.
"""
import builtins
from datetime import datetime
from typing import Any

from app.platform.sql_store import fetch_dicts, get_connection


class PgSessionStore:
    async def touch(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> None:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (thread_id) DO UPDATE SET last_active_at = now()",
                (thread_id, tenant, principal, title, domain),
            )

    async def claim(self, tenant: str, principal: str, thread_id: str, title: str, domain: str) -> bool:
        # `ON CONFLICT DO NOTHING` makes the claim atomic: of two racers for a new id the loser blocks on the unique index until the
        # winner commits, then sees its row.
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (thread_id) DO NOTHING",
                (thread_id, tenant, principal, title, domain),
            )
            cur = await conn.execute(
                "SELECT 1 FROM chat_sessions WHERE thread_id = %s AND tenant = %s "
                "AND principal = %s AND domain = %s",
                (thread_id, tenant, principal, domain),
            )
            return await cur.fetchone() is not None

    async def owns(self, tenant: str, principal: str, thread_id: str, domain: str) -> bool:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT 1 FROM chat_sessions WHERE thread_id = %s AND tenant = %s "
                "AND principal = %s AND domain = %s",
                (thread_id, tenant, principal, domain),
            )
            return await cur.fetchone() is not None

    async def list(self, tenant: str, principal: str, domain: str) -> list[dict[str, Any]]:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT thread_id, title, created_at, last_active_at FROM chat_sessions "
                "WHERE tenant = %s AND principal = %s AND domain = %s ORDER BY last_active_at DESC",
                (tenant, principal, domain),
            )
            return await fetch_dicts(cur)

    async def list_inactive(self, before: datetime, limit: int, after: tuple[datetime, str] | None = None) -> builtins.list[dict[str, Any]]:  # `list` is a method of this class: the builtin by name

        # Keyset order on (last_active_at, thread_id): stable when two threads share a timestamp, and a page never repeats a row whether or not the caller
        # deleted the last one. The table's index leads with the tenant, so this reads by time with a scan; fine for a job that runs off-peak.
        columns = "SELECT thread_id, tenant, principal, domain, last_active_at FROM chat_sessions WHERE last_active_at < %s"
        async with get_connection() as conn:
            if after is None:
                cur = await conn.execute(f"{columns} ORDER BY last_active_at, thread_id LIMIT %s", (before, limit))
            else:
                cur = await conn.execute(
                    f"{columns} AND (last_active_at, thread_id) > (%s, %s) ORDER BY last_active_at, thread_id LIMIT %s",
                    (before, after[0], after[1], limit),
                )
            return await fetch_dicts(cur)

    async def sample_active(
        self, start: datetime, end: datetime, limit: int, tenants: tuple[str, ...] = ()
    ) -> builtins.list[dict[str, Any]]:  # `list` is a method of this class: the builtin by name
        sql = "SELECT thread_id, tenant, principal, domain, last_active_at FROM chat_sessions WHERE last_active_at >= %s AND last_active_at < %s"
        params: tuple[Any, ...] = (start, end)
        if tenants:
            sql += " AND tenant = ANY(%s)"
            params += (list(tenants),)
        # random() over the window's rows: a daily off-peak job over one day's conversations, not a request-path query.
        async with get_connection() as conn:
            cur = await conn.execute(sql + " ORDER BY random() LIMIT %s", (*params, limit))
            return await fetch_dicts(cur)

    async def delete(
        self, tenant: str, principal: str, thread_id: str, domain: str, *, inactive_before: datetime | None = None
    ) -> bool:
        sql = "DELETE FROM chat_sessions WHERE thread_id = %s AND tenant = %s AND principal = %s AND domain = %s"
        params: tuple[Any, ...] = (thread_id, tenant, principal, domain)
        if inactive_before is not None:
            sql += " AND last_active_at < %s"
            params += (inactive_before,)
        async with get_connection() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount > 0
