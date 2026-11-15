"""Deleting a conversation against a REAL Postgres, directly and through a real PgBouncer in transaction mode (PRODUCTION_REVIEW.md R11).

`AsyncPostgresSaver.adelete_thread` runs its three DELETEs in a psycopg pipeline, which is the kind of thing a transaction pooler can break, so the same
checks run through the shipped pooler (`tests/containers.py::ensure_pgbouncer`) as well as straight to Postgres. They prove:

- a thread's rows are gone from all three of the saver's tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`), and another thread's are not;
- the real sweep, over the real session directory and the real saver, removes what is old and keeps what is not, paging through a backlog with tied
  timestamps with the keyset the hermetic tests only pin as SQL text.

Needs Docker (self-skips without it)."""
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from langgraph.checkpoint.base import empty_checkpoint

from app.adapters.outbound.postgres import checkpointing as pg_checkpointing
from app.adapters.outbound.postgres import sessions as pg_sessions
from app.agent.runtime.retention import sweep_inactive_conversations
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.sessions import session_store
from tests.containers import ensure_pgbouncer, ensure_postgres

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("pgbouncer")]

TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


@pytest.fixture(params=["direct", "through-the-pooler"])
def urls(request) -> dict[str, str]:
    """The checkpointer database's URL as the app would use it, and a direct one for counting rows."""
    postgres = ensure_postgres()
    if request.param == "direct":
        return {"app": postgres["checkpointer_database_url"], "direct": postgres["checkpointer_database_url"], "appdata": postgres["appdata_database_url"]}
    pooler = ensure_pgbouncer()
    return {"app": pooler["checkpointer_database_url"], "direct": postgres["checkpointer_database_url"], "appdata": postgres["appdata_database_url"]}


@pytest.fixture
async def adapter(urls, monkeypatch):
    monkeypatch.setattr(pg_checkpointing, "CHECKPOINTER_DATABASE_URL", urls["app"])
    adapter = pg_checkpointing.PgCheckpointing()
    await adapter.open()
    yield adapter
    await adapter.close()


async def _write_thread(saver, thread_id: str, steps: int = 3) -> None:
    """`steps` checkpoints with a message list in a channel (so a blob is stored, as every real turn does) and one pending write."""
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    for step in range(steps):
        checkpoint = {**empty_checkpoint(), "channel_values": {"messages": [f"message {i}" for i in range(step + 1)]}, "channel_versions": {"messages": str(step + 1)}}
        config = await saver.aput(config, checkpoint, {"source": "loop", "step": step, "writes": {}, "parents": {}}, {"messages": str(step + 1)})
    await saver.aput_writes(config, [("messages", "pending")], task_id=f"task-{thread_id}")


async def _rows(direct_url: str, thread_id: str) -> dict[str, int]:
    async with await psycopg.AsyncConnection.connect(direct_url, autocommit=True) as conn:
        return {
            table: (await (await conn.execute(f"SELECT count(*) FROM {table} WHERE thread_id = %s", (thread_id,))).fetchone())[0]
            for table in TABLES
        }


async def test_deleting_a_thread_removes_its_rows_from_all_three_tables_and_only_its_own(adapter, urls):
    saver = adapter._saver
    mine, other = f"retention-{uuid.uuid4().hex}", f"retention-{uuid.uuid4().hex}"
    await _write_thread(saver, mine)
    await _write_thread(saver, other, steps=2)
    before = await _rows(urls["direct"], mine)
    assert all(count > 0 for count in before.values()), f"the setup wrote nothing to one of the tables: {before}"
    other_before = await _rows(urls["direct"], other)

    await adapter.delete_thread(mine)

    assert await _rows(urls["direct"], mine) == dict.fromkeys(TABLES, 0)
    assert await _rows(urls["direct"], other) == other_before, "another conversation's rows are untouched"


async def test_deleting_a_thread_that_has_no_rows_is_not_an_error_and_can_be_repeated(adapter):
    thread = f"retention-{uuid.uuid4().hex}"

    await adapter.delete_thread(thread)
    await adapter.delete_thread(thread)


async def test_the_sweep_over_the_real_session_directory_and_saver_removes_what_is_old_and_keeps_what_is_not(adapter, urls, monkeypatch):
    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(urls["appdata"], autocommit=True) as conn:
            yield conn

    monkeypatch.setattr(pg_sessions, "get_connection", get_connection)
    tenant = f"tenant-{uuid.uuid4().hex[:8]}"  # a tenant of this test's own: the sweep spans tenants, so only these rows may be assumed
    stamp = datetime.now(UTC) - timedelta(days=60)
    old = [f"old-{uuid.uuid4().hex}" for _ in range(5)]
    fresh = f"fresh-{uuid.uuid4().hex}"
    async with get_connection() as conn:
        for thread in old:  # the SAME timestamp for all of them: the thread id has to break the tie when a page ends between two
            await conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain, last_active_at) VALUES (%s, %s, 'alice', 't', 'ecorp', %s)",
                (thread, tenant, stamp),
            )
        await conn.execute("INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain) VALUES (%s, %s, 'alice', 't', 'ecorp')", (fresh, tenant))
    for thread in (*old, fresh):
        await _write_thread(adapter._saver, thread, steps=1)

    with session_store.overriding(pg_sessions.PgSessionStore()), checkpointing.overriding(adapter):
        result = await sweep_inactive_conversations(retention_days=30, batch_size=2)

    assert result.deleted >= 5 and result.revived == 0 and result.complete
    for thread in old:
        assert await _rows(urls["direct"], thread) == dict.fromkeys(TABLES, 0)
    assert all(count > 0 for count in (await _rows(urls["direct"], fresh)).values()), "a conversation inside its retention keeps its checkpoints"
    async with get_connection() as conn:
        left = [row[0] for row in await (await conn.execute("SELECT thread_id FROM chat_sessions WHERE tenant = %s", (tenant,))).fetchall()]
    assert left == [fresh]
