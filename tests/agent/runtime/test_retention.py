"""`sweep_inactive_conversations` (`app/agent/runtime/retention.py`, PRODUCTION_REVIEW.md R11): a conversation is deleted a number of days after its last message.

The cases that matter are the ones an order or a race decides: a crash between the two deletes must leave something the next run finds, a conversation that
wakes up mid-sweep must keep its row, and a dry run must neither delete nor loop."""
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.runtime import retention
from app.core import metrics
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.sessions import session_store
from tests.conftest import metric_value
from tests.fakes.sessions import InMemoryCheckpointing, InMemorySessionStore

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def _ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


@pytest.fixture
def world():
    log: list[str] = []
    sessions, saver = InMemorySessionStore(log), InMemoryCheckpointing(log)
    saver.opened = True
    with session_store.overriding(sessions), checkpointing.overriding(saver):
        yield sessions, saver, log


async def _sweep(**kwargs):
    return await retention.sweep_inactive_conversations(retention_days=kwargs.pop("retention_days", 30), now=NOW, **kwargs)


async def test_a_conversation_older_than_the_retention_loses_its_checkpoints_and_its_row_and_a_newer_one_keeps_both(world):
    sessions, saver, _ = world
    sessions.add("old", last_active_at=_ago(31))
    sessions.add("fresh", last_active_at=_ago(29))

    result = await _sweep()

    assert (result.deleted, result.revived, result.complete) == (1, 0, True)
    assert saver.deleted == ["old"] and set(sessions.rows) == {"fresh"}


async def test_the_checkpoints_go_first_and_the_row_second(world):
    sessions, _, log = world
    sessions.add("old", last_active_at=_ago(40))

    await _sweep()

    assert log == ["checkpoints:old", "row:old"]


async def test_a_crash_between_the_two_deletes_leaves_a_row_the_next_run_removes(world):
    """The order is what makes this recoverable: the row survives, so the next run lists the conversation again (deleting nothing twice, because
    `delete_thread` is idempotent). The other order would leave checkpoints that nothing points at."""
    sessions, saver, _ = world
    sessions.add("old", last_active_at=_ago(40))
    sessions.fail_delete = ConnectionError("appdata went away")

    with pytest.raises(ConnectionError):
        await _sweep()
    assert "old" in sessions.rows and saver.deleted == ["old"]

    sessions.fail_delete = None
    result = await _sweep()

    assert result.deleted == 1 and "old" not in sessions.rows


async def test_a_checkpointer_that_cannot_be_reached_stops_the_run_and_keeps_the_row(world):
    sessions, saver, _ = world
    sessions.add("old", last_active_at=_ago(40))
    saver.fail_for = {"old"}

    with pytest.raises(ConnectionError):
        await _sweep()

    assert "old" in sessions.rows, "the row is what the next run finds the conversation by"


async def test_a_conversation_that_wakes_up_during_the_sweep_keeps_its_row_and_is_counted_as_revived(world):
    sessions, saver, _ = world
    sessions.add("wakes", last_active_at=_ago(40))
    sessions.add("sleeps", last_active_at=_ago(41))
    saver.on_delete = lambda thread: sessions.rows[thread].update(last_active_at=NOW) if thread == "wakes" else None

    result = await _sweep()

    assert (result.deleted, result.revived) == (1, 1)
    assert set(sessions.rows) == {"wakes"}


async def test_a_dry_run_counts_and_deletes_nothing_and_needs_no_checkpointer(world):
    sessions, saver, log = world
    saver.opened = False  # a dry run must not reach it: delete_thread would raise
    for n in range(3):
        sessions.add(f"old-{n}", last_active_at=_ago(40 + n))
    sessions.add("fresh", last_active_at=_ago(1))

    result = await _sweep(dry_run=True)

    assert (result.deleted, result.dry_run) == (3, True)
    assert len(sessions.rows) == 4 and saver.deleted == [] and log == []


async def test_a_dry_run_walks_every_page_instead_of_reading_the_first_one_for_ever(world):
    """Nothing is deleted in a dry run, so a page that is "the oldest N still there" would be the same page every time. The listing is keyset-paged."""
    sessions, _, _ = world
    for n in range(7):
        sessions.add(f"old-{n}", last_active_at=_ago(60))  # all the SAME timestamp: the thread id breaks the tie

    result = await _sweep(dry_run=True, batch_size=3)

    assert (result.deleted, result.complete) == (7, True)


async def test_every_page_of_a_large_backlog_is_swept(world):
    sessions, saver, _ = world
    for n in range(25):
        sessions.add(f"old-{n:02d}", last_active_at=_ago(40 + n))

    result = await _sweep(batch_size=10)

    assert (result.deleted, result.complete) == (25, True)
    assert sessions.rows == {} and len(saver.deleted) == 25


async def test_the_ceiling_stops_a_run_and_says_so_and_the_next_run_finishes(world):
    sessions, _, _ = world
    for n in range(25):
        sessions.add(f"old-{n:02d}", last_active_at=_ago(40 + n))

    first = await _sweep(batch_size=10, max_batches=2)
    second = await _sweep(batch_size=10, max_batches=2)

    assert (first.deleted, first.complete) == (20, False)
    assert (second.deleted, second.complete) == (5, True)


async def test_it_spans_tenants_and_domains_because_retention_is_a_property_of_the_table(world):
    sessions, _, _ = world
    sessions.add("a", tenant="acme", principal="alice", domain="ecorp", last_active_at=_ago(40))
    sessions.add("b", tenant="globex", principal="bob", domain="sales", last_active_at=_ago(40))

    assert (await _sweep()).deleted == 2


async def test_a_conversation_is_removed_only_through_its_own_tenant_person_and_domain(world):
    sessions, _, _ = world
    sessions.add("a", tenant="acme", principal="alice", domain="ecorp", last_active_at=_ago(40))

    assert await sessions.delete("globex", "alice", "a", "ecorp") is False
    assert await sessions.delete("acme", "mallory", "a", "ecorp") is False
    assert await sessions.delete("acme", "alice", "a", "sales") is False
    assert "a" in sessions.rows


@pytest.mark.parametrize("days", [0, -1])
async def test_fewer_than_one_day_is_refused_before_anything_is_touched(world, days):
    sessions, saver, log = world
    sessions.add("old", last_active_at=_ago(400))

    with pytest.raises(ValueError, match="at least 1"):
        await _sweep(retention_days=days)

    assert "old" in sessions.rows and log == []


async def test_the_deleted_conversations_are_counted_and_a_dry_run_is_not(world):
    sessions, _, _ = world
    sessions.add("a", last_active_at=_ago(40))
    sessions.add("b", last_active_at=_ago(40))
    before = metric_value(metrics.agent_checkpoint_sweep_deleted_total)

    await _sweep(dry_run=True)
    assert metric_value(metrics.agent_checkpoint_sweep_deleted_total) == before

    await _sweep()
    assert metric_value(metrics.agent_checkpoint_sweep_deleted_total) == before + 2


async def test_a_second_run_with_nothing_new_to_sweep_deletes_nothing(world):
    sessions, _, _ = world
    sessions.add("old", last_active_at=_ago(40))
    await _sweep()

    again = await _sweep()

    assert (again.deleted, again.revived, again.complete) == (0, 0, True)
