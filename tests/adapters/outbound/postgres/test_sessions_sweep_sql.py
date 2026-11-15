"""The statements `PgSessionStore.list_inactive` and `delete` run (PRODUCTION_REVIEW.md R11), and `PgCheckpointing.delete_thread`.

These pin the SQL text and parameters (a fake connection cannot prove a real constraint; `tests/integration/test_checkpoint_retention_real_postgres.py` runs the
same statements against Postgres)."""
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import pytest

from app.adapters.outbound.postgres import checkpointing as pg_checkpointing
from app.adapters.outbound.postgres import sessions as pg_sessions

BEFORE = datetime(2026, 11, 1, tzinfo=UTC)
LAST = datetime(2026, 10, 1, tzinfo=UTC)


class _Cursor:
    def __init__(self, rows, columns, rowcount):
        self._rows, self.rowcount = rows, rowcount
        self.description = [type("Col", (), {"name": c}) for c in columns]

    async def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows=(), columns=(), rowcount=0):
        self.calls: list[tuple[str, tuple]] = []
        self._rows, self._columns, self._rowcount = rows, columns, rowcount

    async def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), tuple(params)))
        return _Cursor(self._rows, self._columns, self._rowcount)


def _bind(monkeypatch, connection):
    @asynccontextmanager
    async def get_connection():
        yield connection

    monkeypatch.setattr(pg_sessions, "get_connection", get_connection)


async def test_the_first_page_of_inactive_threads_is_the_oldest_first_across_every_tenant(monkeypatch):
    columns = ("thread_id", "tenant", "principal", "domain", "last_active_at")
    connection = _Connection(rows=[("t1", "acme", "alice", "ecorp", LAST)], columns=columns)
    _bind(monkeypatch, connection)

    rows = await pg_sessions.PgSessionStore().list_inactive(BEFORE, 100)

    ((sql, params),) = connection.calls
    assert "WHERE last_active_at < %s ORDER BY last_active_at, thread_id LIMIT %s" in sql
    assert "tenant = %s" not in sql.split("WHERE")[1], "spans tenants on purpose: retention is a property of the table"
    assert params == (BEFORE, 100)
    assert rows == [dict(zip(columns, ("t1", "acme", "alice", "ecorp", LAST), strict=True))]


async def test_a_later_page_continues_after_the_last_row_by_time_and_thread_id(monkeypatch):
    connection = _Connection()
    _bind(monkeypatch, connection)

    await pg_sessions.PgSessionStore().list_inactive(BEFORE, 50, after=(LAST, "t1"))

    ((sql, params),) = connection.calls
    assert "AND (last_active_at, thread_id) > (%s, %s) ORDER BY last_active_at, thread_id LIMIT %s" in sql
    assert params == (BEFORE, LAST, "t1", 50)


@pytest.mark.parametrize("removed", [1, 0])
async def test_a_delete_is_scoped_to_the_thread_tenant_person_and_domain_and_says_whether_a_row_went(monkeypatch, removed):
    connection = _Connection(rowcount=removed)
    _bind(monkeypatch, connection)

    assert await pg_sessions.PgSessionStore().delete("acme", "alice", "t1", "ecorp") is bool(removed)

    ((sql, params),) = connection.calls
    assert sql == "DELETE FROM chat_sessions WHERE thread_id = %s AND tenant = %s AND principal = %s AND domain = %s"
    assert params == ("t1", "acme", "alice", "ecorp")


async def test_a_delete_with_inactive_before_only_removes_a_row_that_is_still_inactive(monkeypatch):
    connection = _Connection(rowcount=1)
    _bind(monkeypatch, connection)

    await pg_sessions.PgSessionStore().delete("acme", "alice", "t1", "ecorp", inactive_before=BEFORE)

    ((sql, params),) = connection.calls
    assert sql.endswith("AND domain = %s AND last_active_at < %s")
    assert params == ("t1", "acme", "alice", "ecorp", BEFORE)


class _Saver:
    def __init__(self):
        self.deleted = []

    async def adelete_thread(self, thread_id):
        self.deleted.append(thread_id)


async def test_deleting_a_thread_before_the_checkpointer_is_open_is_an_error_not_a_silent_no_op():
    with pytest.raises(RuntimeError, match="before open"):
        await pg_checkpointing.PgCheckpointing().delete_thread("t1")


async def test_deleting_a_thread_goes_through_the_saver_it_opened():
    adapter = pg_checkpointing.PgCheckpointing()
    saver = _Saver()
    adapter._saver = saver  # type: ignore[assignment]  # what open() leaves behind

    await adapter.delete_thread("t1")

    assert saver.deleted == ["t1"]


async def test_closing_forgets_the_saver_so_a_later_delete_cannot_use_a_closed_pool():
    adapter = pg_checkpointing.PgCheckpointing()
    adapter._saver = _Saver()  # type: ignore[assignment]

    await adapter.close()

    with pytest.raises(RuntimeError):
        await adapter.delete_thread("t1")


async def test_the_answer_sample_is_a_random_slice_of_one_window_across_tenants_unless_limited(monkeypatch):
    columns = ("thread_id", "tenant", "principal", "domain", "last_active_at")
    connection = _Connection(rows=[("t1", "acme", "alice", "ecorp", LAST)], columns=columns)
    _bind(monkeypatch, connection)

    rows = await pg_sessions.PgSessionStore().sample_active(LAST, BEFORE, 50)

    ((sql, params),) = connection.calls
    assert "WHERE last_active_at >= %s AND last_active_at < %s ORDER BY random() LIMIT %s" in sql and "tenant" not in sql.split("WHERE")[1]
    assert params == (LAST, BEFORE, 50)
    assert rows == [dict(zip(columns, ("t1", "acme", "alice", "ecorp", LAST), strict=True))]


async def test_the_answer_sample_can_be_limited_to_named_tenants(monkeypatch):
    connection = _Connection()
    _bind(monkeypatch, connection)

    await pg_sessions.PgSessionStore().sample_active(LAST, BEFORE, 50, tenants=("acme", "globex"))

    ((sql, params),) = connection.calls
    assert "AND tenant = ANY(%s) ORDER BY random() LIMIT %s" in sql
    assert params == (LAST, BEFORE, ["acme", "globex"], 50)
