"""`SelfHealingPool` (`app/platform/self_healing_pool.py`, PRODUCTION_REVIEW.md R25): what does, and does not, make a pool replace its dead connections.

The behaviour against a real server whose connections are all killed is `tests/integration/test_pool_survives_a_restart_real_postgres.py`'s. This is the decision: it acts on a
connection that is actually broken, once at a time, and never changes what the failing caller sees."""
import asyncio
import contextlib
from types import SimpleNamespace

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from app.core import metrics
from app.platform.self_healing_pool import SelfHealingPool
from tests.conftest import metric_value


class _Conn(SimpleNamespace):
    pass


@pytest.fixture
def pool(monkeypatch):
    """A SelfHealingPool with no server: the parent's `connection()` hands out `conn` (set per test) and `check()` is a counter."""
    state = SimpleNamespace(conn=_Conn(broken=True), checks=0, check_started=asyncio.Event(), release_check=asyncio.Event(), block_check=False, fail_check=False)

    @contextlib.asynccontextmanager
    async def parent_connection(self, timeout=None):
        yield state.conn

    async def check(self):
        state.checks += 1
        state.check_started.set()
        if state.block_check:
            await state.release_check.wait()
        if state.fail_check:
            raise ConnectionError("the check itself failed")

    monkeypatch.setattr(AsyncConnectionPool, "connection", parent_connection)
    monkeypatch.setattr(SelfHealingPool, "check", check)
    instance = object.__new__(SelfHealingPool)
    instance.name = "appdata"
    instance.state = state  # type: ignore[attr-defined]
    return instance


async def _fail_with(pool, exc):
    with pytest.raises(type(exc)) as caught:
        async with pool.connection():
            raise exc
    return caught.value


async def _settle():
    await asyncio.sleep(0)
    await asyncio.sleep(0)


async def test_a_broken_connection_makes_the_pool_check_all_its_connections_once(pool):
    await _fail_with(pool, psycopg.OperationalError("server closed the connection unexpectedly"))
    await asyncio.wait_for(pool.state.check_started.wait(), 1)
    await _settle()

    assert pool.state.checks == 1


async def test_the_caller_sees_exactly_what_it_would_have_seen_without_the_healing(pool):
    original = psycopg.OperationalError("server closed the connection unexpectedly")

    raised = await _fail_with(pool, original)

    assert raised is original


@pytest.mark.parametrize("exc", [psycopg.OperationalError("lost"), psycopg.InterfaceError("the connection is closed")])
async def test_both_kinds_of_connection_error_count(pool, exc):
    await _fail_with(pool, exc)
    await _settle()

    assert pool.state.checks == 1


async def test_a_query_that_failed_on_a_healthy_connection_does_not_trigger_a_check(pool):
    """A deadlock, a cancelled statement or a constraint is not a dead pool: checking every connection for each would be a load of its own."""
    pool.state.conn = _Conn(broken=False)

    await _fail_with(pool, psycopg.OperationalError("canceling statement due to statement timeout"))
    await _settle()

    assert pool.state.checks == 0


async def test_a_pool_that_is_merely_exhausted_is_not_a_dead_one(pool, monkeypatch):
    @contextlib.asynccontextmanager
    async def never_given(self, timeout=None):
        raise PoolTimeout("couldn't get a connection after 30.00 sec")
        yield  # pragma: no cover

    monkeypatch.setattr(AsyncConnectionPool, "connection", never_given)

    with pytest.raises(PoolTimeout):
        async with pool.connection():
            pass
    await _settle()

    assert pool.state.checks == 0


async def test_an_error_that_is_not_the_databases_passes_through_untouched(pool):
    await _fail_with(pool, ValueError("a bug in the caller"))
    await _settle()

    assert pool.state.checks == 0


async def test_a_call_that_succeeds_checks_nothing(pool):
    async with pool.connection() as conn:
        assert conn is pool.state.conn
    await _settle()

    assert pool.state.checks == 0


async def test_many_connections_failing_at_once_start_one_check_not_one_each(pool):
    pool.state.block_check = True

    await asyncio.gather(*(_fail_with(pool, psycopg.OperationalError("lost")) for _ in range(6)))
    await asyncio.wait_for(pool.state.check_started.wait(), 1)
    await _settle()

    assert pool.state.checks == 1, "six dead connections found out together; the first check covers them all"
    pool.state.release_check.set()


async def test_after_a_check_finishes_a_later_failure_starts_another(pool):
    await _fail_with(pool, psycopg.OperationalError("lost"))
    await _settle()
    await _fail_with(pool, psycopg.OperationalError("lost again"))
    await _settle()

    assert pool.state.checks == 2


async def test_every_lost_connection_is_counted_even_while_a_check_is_running(pool):
    pool.state.block_check = True
    before = metric_value(metrics.agent_db_pool_connection_lost_total, pool="appdata")

    await asyncio.gather(*(_fail_with(pool, psycopg.OperationalError("lost")) for _ in range(3)))
    pool.state.release_check.set()

    assert metric_value(metrics.agent_db_pool_connection_lost_total, pool="appdata") == before + 3


async def test_a_check_that_fails_is_logged_not_raised_and_does_not_stop_the_next_one(pool, caplog):
    pool.state.fail_check = True

    with caplog.at_level("WARNING"):
        await _fail_with(pool, psycopg.OperationalError("lost"))
        await _settle()
        await _fail_with(pool, psycopg.OperationalError("lost"))
        await _settle()

    assert pool.state.checks == 2 and any(r.message == "db_pool_check_failed" for r in caplog.records)
