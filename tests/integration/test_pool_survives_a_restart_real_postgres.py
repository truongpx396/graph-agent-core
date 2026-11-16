"""A pool after the database it talks to restarted, against a REAL Postgres (PRODUCTION_REVIEW.md R25).

After PgBouncer or Postgres restarts, every pooled connection is dead and the plain pool finds out one connection at a time: each caller's first query fails. The work order
proposed `check=AsyncConnectionPool.check_connection`; measured here, with `psycopg_pool` 3.3.1, that is worse (the first request waits out the 30 s pool timeout, because the
check loop backs off 1, 2, 4, 8, 16 s between dead connections while the replacements are already ready), so the app's pools replace their dead connections with the pool's own
`check()` once, after the first one fails (`SelfHealingPool`). The restart is simulated by terminating every backend of the role, which kills every pooled connection the way a
restart does. This runs the app's real pools: the appdata one and the checkpointer's.

Self-skips (never fails) without Docker."""
import asyncio
import time

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from app.adapters.outbound.postgres import checkpointing as pg_checkpointing
from app.platform import sql_store
from tests.containers import ensure_postgres

pytestmark = pytest.mark.integration

POOL_SIZE = 8


@pytest.fixture
def urls() -> dict[str, str]:
    return ensure_postgres()


async def _kill_every_pooled_connection(url: str) -> None:
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid() AND usename = 'langfuse'"
        )
    await asyncio.sleep(0.3)


async def _warm(pool: AsyncConnectionPool) -> None:
    async def one() -> None:
        async with pool.connection() as conn:
            await conn.execute("SELECT pg_sleep(0.05)")

    await asyncio.gather(*(one() for _ in range(POOL_SIZE)))  # POOL_SIZE connections open and idle


async def _queries_after_the_kill(pool: AsyncConnectionPool, count: int) -> tuple[int, float]:
    """(how many failed, the slowest), one query after another."""
    failed, slowest = 0, 0.0
    for _ in range(count):
        started = time.perf_counter()
        try:
            async with pool.connection() as conn:
                await conn.execute("SELECT 1")
        except psycopg.Error:
            failed += 1
        slowest = max(slowest, time.perf_counter() - started)
    return failed, slowest


async def test_the_control_a_plain_pool_fails_once_per_dead_connection(urls):
    """The self-check (pattern 88): the same scenario against the stock pool shows the failure the fix is for, so a pass below means something."""
    pool = AsyncConnectionPool(urls["appdata_database_url"], min_size=1, max_size=POOL_SIZE, kwargs=dict(sql_store.POOLER_SAFE_CONNECTION_KWARGS), open=False)
    await pool.open(wait=True)
    try:
        await _warm(pool)
        await _kill_every_pooled_connection(urls["appdata_database_url"])
        failed, _ = await _queries_after_the_kill(pool, POOL_SIZE + 4)
    finally:
        await pool.close()

    assert failed >= POOL_SIZE // 2, f"only {failed} queries failed on a plain pool: the scenario no longer reproduces the problem"


async def test_the_appdata_pool_recovers_after_a_restart_with_a_couple_of_failures_not_one_per_connection(urls, monkeypatch):
    monkeypatch.setattr(sql_store, "APPDATA_DATABASE_URL", urls["appdata_database_url"])
    monkeypatch.setattr(sql_store, "APPDATA_POOL_MAX_SIZE", POOL_SIZE)
    monkeypatch.setattr(sql_store, "_pool", None)
    pool = await sql_store._get_pool()
    try:
        await _warm(pool)
        await _kill_every_pooled_connection(urls["appdata_database_url"])
        failed, slowest = await _queries_after_the_kill(pool, POOL_SIZE + 4)
    finally:
        await sql_store.close_pool()

    assert failed <= 2, f"{failed} of {POOL_SIZE + 4} queries failed: the pool did not replace its dead connections together"
    assert slowest < 5, f"a query took {slowest:.1f} s: the 30 s stall that `check=` causes"


async def test_the_checkpointer_pool_recovers_too(urls, monkeypatch):
    monkeypatch.setattr(pg_checkpointing, "CHECKPOINTER_DATABASE_URL", urls["checkpointer_database_url"])
    monkeypatch.setattr(pg_checkpointing, "CHECKPOINTER_POOL_MAX_SIZE", POOL_SIZE)
    adapter = pg_checkpointing.PgCheckpointing()
    saver = await adapter.open()
    pool = saver.conn
    try:
        await _warm(pool)
        await _kill_every_pooled_connection(urls["checkpointer_database_url"])
        failed, slowest = await _queries_after_the_kill(pool, POOL_SIZE + 4)
    finally:
        await adapter.close()

    assert failed <= 2 and slowest < 5, f"{failed} failed, slowest {slowest:.1f} s"
