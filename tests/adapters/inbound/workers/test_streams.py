"""The Streams mechanics the two workers share (app/adapters/inbound/workers/streams.py), against the hand-rolled FakeRedis.

What a real server adds is in tests/integration/test_queue_real_redis.py.
"""
import asyncio

import pytest

from app.adapters.inbound.workers import streams
from app.platform import redis_client
from tests.adapters.outbound.redis.test_queue import FakeRedis

STREAM, GROUP = "agent:requests:test", "workers-test"


class TestPublishResult:
    """An answer of 500 tokens was 1,500 sequential round trips: the cancel check, an XADD and an EXPIRE for each event (PRODUCTION_REVIEW.md R08, step 1)."""

    async def test_an_event_and_the_expiry_of_its_stream_and_its_owner_are_one_pipelined_round_trip(self):
        """The second EXPIRE keeps the conversation a request id belongs to alive as long as the stream is (what authorizes `GET /chat/stream/{request_id}`, R13)."""
        client = FakeRedis()

        await streams.publish_result(client, "req-1", {"type": "token", "content": "hi"})

        assert client.pipeline_runs == [(True, ["xadd", "expire", "expire"])]
        assert client.expiries["agent:results:req-1"] == streams.RESULTS_STREAM_TTL_SECONDS
        assert client.expiries["agent:request_thread:req-1"] == streams.RESULTS_STREAM_TTL_SECONDS
        assert len(client.streams["agent:results:req-1"]) == 1

    async def test_every_event_of_a_turn_is_one_round_trip_and_the_ttl_is_refreshed_on_each(self):
        client = FakeRedis()

        for event in ({"type": "token", "content": "a"}, {"type": "token", "content": "b"}, {"type": "done"}):
            await streams.publish_result(client, "req-2", event)

        assert client.pipeline_runs == [(True, ["xadd", "expire", "expire"])] * 3

    async def test_the_ingest_workers_publisher_is_the_same(self):
        from app.adapters.inbound.workers import ingest_streams

        client = FakeRedis()

        await ingest_streams.publish_result(client, "job-1", {"type": "started"})

        assert client.pipeline_runs == [(True, ["xadd", "expire"])]
        assert client.expiries["ingest:results:job-1"] == streams.RESULTS_STREAM_TTL_SECONDS


class TestAckAndDelete:
    async def test_it_acknowledges_and_removes_the_entry_in_one_transaction(self):
        """Real gap (read from the code, and `XACK` alone is documented to leave the entry in the stream): every message ever
        sent, with its text and any attached images as data URIs, stayed in Redis, which has a 256 MB limit in production
        (PRODUCTION_REVIEW.md R04)."""
        client = FakeRedis()
        entry_id = await client.xadd(STREAM, {"payload": "{}"})
        other_id = await client.xadd(STREAM, {"payload": "{}"})

        await streams.ack_and_delete(client, STREAM, GROUP, entry_id)

        assert client.acked == [entry_id]
        assert [eid for eid, _ in client.streams[STREAM]] == [other_id]
        assert client.pipeline_runs == [(True, ["xack", "xdel"])], "one MULTI/EXEC: a process that dies between two commands leaves neither half"


class TestTrimStaleEntries:
    async def test_it_drops_what_is_older_than_the_age_and_keeps_the_rest(self, monkeypatch):
        monkeypatch.setattr(streams.time, "time", lambda: 1_000_000.0)
        client = FakeRedis()
        client.streams[STREAM] = [("1-0", {}), ("913599999-0", {}), ("913600000-0", {}), ("999999999-0", {})]

        await streams.trim_stale_entries(client, STREAM, max_age_seconds=86_400)

        assert client.trims == [(STREAM, "913600000-0")]  # (1_000_000 - 86_400) s as a millisecond id
        assert [eid for eid, _ in client.streams[STREAM]] == ["913600000-0", "999999999-0"]


class TestReleaseConsumer:
    async def test_a_consumer_holding_nothing_is_removed_from_the_group(self):
        client = FakeRedis()
        entry_id = await client.xadd(STREAM, {"payload": "{}"})
        await client.xreadgroup(GROUP, "me", {STREAM: ">"})
        await streams.ack_and_delete(client, STREAM, GROUP, entry_id)

        await streams.release_consumer(client, STREAM, GROUP, "me")

        assert client.deleted_consumers == [(STREAM, GROUP, "me")]

    async def test_a_consumer_that_still_owns_an_unacknowledged_entry_is_left_alone(self):
        """XGROUP DELCONSUMER discards the consumer's pending entries, and an entry nobody owns is never read or reclaimed again."""
        client = FakeRedis()
        await client.xadd(STREAM, {"payload": "{}"})
        await client.xreadgroup(GROUP, "me", {STREAM: ">"})

        await streams.release_consumer(client, STREAM, GROUP, "me")

        assert client.deleted_consumers == []

    async def test_a_consumer_the_group_never_heard_of_is_a_no_op(self):
        client = FakeRedis()

        await streams.release_consumer(client, STREAM, GROUP, "never-read")

        assert client.deleted_consumers == []

    async def test_a_failure_is_reported_not_raised(self):
        """It runs while the process is stopping: it must not keep the pools from closing."""

        class _Broken(FakeRedis):
            async def xinfo_consumers(self, stream, group):
                raise ConnectionError("redis went away")

        await streams.release_consumer(_Broken(), STREAM, GROUP, "me")


@pytest.mark.parametrize("builtin_or_redis", ["builtin", "redis"])
def test_the_errors_a_blocking_read_may_raise_name_both_the_redis_and_the_os_classes(builtin_or_redis):
    """redis-py's ConnectionError and TimeoutError are RedisErrors, not OSErrors, so a tuple of OSError alone would miss a dropped connection."""
    import redis.asyncio as redis

    sample = ConnectionResetError() if builtin_or_redis == "builtin" else redis.ConnectionError()

    assert isinstance(sample, redis_client.UNAVAILABLE_ERRORS)
    assert isinstance(redis.TimeoutError(), redis_client.UNAVAILABLE_ERRORS)
    assert not isinstance(redis.ResponseError("WRONGTYPE"), redis_client.UNAVAILABLE_ERRORS), "a rejected command is not a blip"


async def test_the_pause_after_a_failed_read_doubles_up_to_a_cap():
    stop = asyncio.Event()
    assert await streams.pause_after_failed_read(stop, 0.001) == 0.002  # waits its 1 ms

    stop.set()  # from here the wait ends at once; the delay it returns is what is under test
    assert await streams.pause_after_failed_read(stop, 1.0) == 2.0
    assert await streams.pause_after_failed_read(stop, streams.READ_BACKOFF_MAX_SECONDS) == streams.READ_BACKOFF_MAX_SECONDS


async def test_a_worker_told_to_stop_is_not_kept_waiting_out_a_backoff():
    stop = asyncio.Event()
    stop.set()
    started = asyncio.get_running_loop().time()

    await streams.pause_after_failed_read(stop, 60.0)

    assert asyncio.get_running_loop().time() - started < 1


class TestClaimFreeSlots:
    """A worker used to read up to its whole concurrency from the queue and only then wait on the semaphore for each entry. A claimed entry's idle clock
    runs while it waits for a slot, so under saturation "idle" measured the queue, not a dead worker, and a healthy job was reclaimed (R05, pattern 99)."""

    async def test_it_takes_every_slot_that_is_free_without_waiting(self):
        semaphore = asyncio.Semaphore(4)
        await semaphore.acquire()  # one is busy

        held = await streams.claim_free_slots(semaphore, asyncio.Event(), limit=4, wait_seconds=1)

        assert held == 3
        assert semaphore.locked(), "all of them are now held by the caller"

    async def test_it_never_takes_more_than_the_limit(self):
        semaphore = asyncio.Semaphore(10)

        assert await streams.claim_free_slots(semaphore, asyncio.Event(), limit=2, wait_seconds=1) == 2

    async def test_with_none_free_it_waits_for_one_and_takes_just_that(self):
        semaphore = asyncio.Semaphore(2)
        await semaphore.acquire()
        await semaphore.acquire()
        claim = asyncio.create_task(streams.claim_free_slots(semaphore, asyncio.Event(), limit=2, wait_seconds=5))
        await asyncio.sleep(0.02)
        assert not claim.done(), "no slot, so nothing to read for"

        semaphore.release()

        assert await asyncio.wait_for(claim, 1) == 1

    async def test_a_stop_while_waiting_returns_zero_and_holds_nothing(self):
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        stop = asyncio.Event()
        claim = asyncio.create_task(streams.claim_free_slots(semaphore, stop, limit=1, wait_seconds=0.01))
        await asyncio.sleep(0.03)

        stop.set()

        assert await asyncio.wait_for(claim, 1) == 0
        semaphore.release()
        assert not semaphore.locked(), "the slot the busy job freed was not swallowed by the abandoned wait"

    async def test_a_stop_that_arrives_with_a_slot_gives_the_slot_back(self):
        semaphore = asyncio.Semaphore(2)
        stop = asyncio.Event()
        stop.set()

        assert await streams.claim_free_slots(semaphore, stop, limit=2, wait_seconds=1) == 0
        assert not semaphore.locked()


    async def test_a_wait_for_a_slot_ticks_so_a_worker_with_every_slot_busy_still_shows_it_is_alive(self):
        semaphore = asyncio.Semaphore(1)
        await semaphore.acquire()
        ticks = []
        claim = asyncio.create_task(streams.claim_free_slots(semaphore, asyncio.Event(), limit=1, wait_seconds=0.01, on_wait=lambda: ticks.append(1)))

        await asyncio.sleep(0.08)
        semaphore.release()

        assert await asyncio.wait_for(claim, 1) == 1
        assert len(ticks) >= 3
