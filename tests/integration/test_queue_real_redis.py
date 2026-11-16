"""A real Redis Stack round trip for app/adapters/outbound/redis/queue.py — the fake-free
counterpart to tests/adapters/outbound/redis/test_queue.py, which (correctly, for a fast/
hermetic suite) drives the exact same functions against a hand-rolled
`FakeRedis` covering just the Streams commands this app uses. What that
can't catch — a real redis-py/Redis Stack server version mismatch in
consumer-group semantics (`XGROUP CREATE`/`XREADGROUP`/`XACK`) breaking
silently across a dependency bump — needs an actual server, hence
`@pytest.mark.integration` (see tests/containers.py's own docstring for why
this self-skips without Docker rather than needing `make up`).

Drives the real producer/consumer functions end to end for the "turn" job
kind: `publish_request` (producer) → a real `XREADGROUP` pull, the same
shape app/adapters/inbound/workers/agent_worker.py's own dispatch loop reads (consumer) →
`publish_result`/`read_results` for the results leg (worker → producer) —
proving the two real Redis Streams primitives this whole queue is built on
actually round-trip against a real server, not just this app's own
in-memory model of them.
"""
import json
import uuid

import pytest

from app.adapters.inbound.workers import streams
from app.adapters.outbound.redis import queue
from app.platform import redis_client
from app.ports.outbound import jobs
from tests.conftest import TEST_CTX
from tests.containers import ensure_redis
from tests.redis_streams import use_private_requests_stream

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def real_redis(monkeypatch):
    """Points `queue` at a real, shared Redis container (see
    tests/containers.py) — and, since it's SHARED (across tests in this
    file, and across xdist workers running them concurrently), also gives
    each test its OWN requests-stream/`CONSUMER_GROUP` name rather than the
    fixed production ones. A domain's requests stream is shared by every
    "turn"/"resume"/"cancel" job for that domain (and, here, every test) by
    design in production — `XREADGROUP ... ">"` hands out whichever message
    is next in GROUP delivery order, not "whichever message this specific
    test just published," so two tests racing against the fixed names under
    real parallelism could cross-deliver each other's message. Unique
    per-test names are the same isolation idiom
    tests/agent/runtime/test_durable_checkpoint.py already uses via a unique
    thread_id, applied to the two names that stand in for it here.

    Patches `requests_stream_key` itself (not a fixed module constant, now
    that it's domain-parameterized) to ALWAYS return this one unique
    stream regardless of which domain a call passes in — this file only
    ever exercises the default ("ecorp") domain, so collapsing every domain
    onto the same unique test stream changes nothing it actually asserts."""
    info = ensure_redis()
    use_private_requests_stream(monkeypatch, info["redis_url"])
    yield
    redis_client._client = None


async def test_a_turn_request_round_trips_from_producer_through_a_real_consumer_group():
    async def _run():
        client = queue.get_client()
        await streams.ensure_consumer_group(client)

        request_id = str(uuid.uuid4())
        thread_id = str(uuid.uuid4())
        await queue.publish_request(
            client,
            request_id=request_id,
            text="what is 21 * 2?",
            thread_id=thread_id,
            ctx=TEST_CTX,
        )

        # Consumer side: the same XREADGROUP shape app/adapters/inbound/workers/agent_worker.py's
        # own dispatch loop reads (see that module's `run()`).
        response = await client.xreadgroup(
            jobs.CONSUMER_GROUP, "test-consumer", {jobs.requests_stream_key(): ">"}, count=1
        )
        [(_, entries)] = response
        [(entry_id, fields)] = entries
        payload = json.loads(fields["payload"])
        assert isinstance(payload.pop("enqueued_at"), float)  # stamped by the API: the worker's queue wait is measured from it (R17)
        assert payload == {
            "kind": "turn",
            "request_id": request_id,
            "text": "what is 21 * 2?",
            "thread_id": thread_id,
            "ctx": TEST_CTX,
            "require_approval": False,
            "images": [],
            "unattended": False,
        }
        await client.xack(jobs.requests_stream_key(), jobs.CONSUMER_GROUP, entry_id)

        # Results leg: the worker publishes events, the producer drains them
        # via read_results until a terminal one.
        await streams.publish_result(client, request_id, {"type": "token", "content": "42"})
        await streams.publish_result(client, request_id, {"type": "done"})

        return [event async for event in queue.read_results(client, request_id)]

    events = await _run()

    assert events == [{"type": "token", "content": "42"}, {"type": "done"}]


async def test_a_second_worker_in_the_same_group_never_gets_a_message_already_delivered():
    """The whole reason this queue uses a Redis Streams CONSUMER GROUP (not a plain stream every worker reads
    independently) — "Redis's own group semantics guarantee each request is delivered to exactly one worker,
    so running N worker processes is the entire scaling story" per this module's own docstring.
    """

    async def _run():
        client = queue.get_client()
        await streams.ensure_consumer_group(client)
        await queue.publish_request(
            client, request_id="req-2", text="hi", thread_id="thread-2", ctx=TEST_CTX
        )

        first = await client.xreadgroup(
            jobs.CONSUMER_GROUP, "worker-a", {jobs.requests_stream_key(): ">"}, count=1
        )
        second = await client.xreadgroup(
            jobs.CONSUMER_GROUP, "worker-b", {jobs.requests_stream_key(): ">"}, count=1, block=100
        )
        return first, second

    first, second = await _run()

    assert first and first[0][1]  # worker-a got the one message
    assert second == [] or second[0][1] == []  # worker-b got nothing new


async def test_a_processed_job_is_removed_from_the_requests_stream_on_real_redis(monkeypatch):
    """`XACK` takes a job off the pending list and leaves it in the stream: the fake could model neither half, a real server shows both are gone
    (PRODUCTION_REVIEW.md R04)."""
    from app.adapters.inbound.workers import agent_worker

    async def fake_turn(text, thread_id, ctx, **kwargs):
        yield {"type": "done"}

    monkeypatch.setattr(agent_worker, "astream_events_turn", fake_turn)
    client = queue.get_client()
    stream = jobs.requests_stream_key()
    await streams.ensure_consumer_group(client)
    await queue.publish_request(client, request_id="req-gone", text="hi", thread_id="thread-gone", ctx=TEST_CTX)
    [(_, [(entry_id, fields)])] = await client.xreadgroup(jobs.CONSUMER_GROUP, agent_worker.CONSUMER_NAME, {stream: ">"}, count=1)
    assert await client.xlen(stream) == 1

    await agent_worker.process_request(client, entry_id, fields, domain="ecorp")

    assert await client.xlen(stream) == 0
    assert (await client.xpending(stream, jobs.CONSUMER_GROUP))["pending"] == 0


async def test_reclaim_still_finds_an_unacknowledged_job_after_trimming():
    """The age trim must never cost the reclaim path a job that could still be recovered: this one is seconds old, delivered to a worker that died."""
    client = queue.get_client()
    stream = jobs.requests_stream_key()
    await streams.ensure_consumer_group(client)
    await queue.publish_request(client, request_id="req-rescue", text="hi", thread_id="thread-rescue", ctx=TEST_CTX)
    [(_, [(entry_id, _fields)])] = await client.xreadgroup(jobs.CONSUMER_GROUP, "dead-worker", {stream: ">"}, count=1)

    await streams.trim_stale_entries(client, stream, max_age_seconds=86_400)
    claimed = await streams.reclaim_stale_entries(client, stream=stream, group=jobs.CONSUMER_GROUP, consumer="rescuer", min_idle_ms=0)

    assert [claimed_id for claimed_id, _ in claimed] == [entry_id]


async def test_entries_older_than_the_age_are_trimmed_and_the_reclaim_pass_copes_with_their_pending_claims():
    """A real server trims by whole internal nodes (about 100 entries each) when asked to be approximate, which bounds the work of one call on a stream
    that grew for months; a stream of a few entries is left alone, so this one is long enough to span several nodes. Redis keeps a pending claim after
    its entry is trimmed, and XAUTOCLAIM reports and drops it (the third element of its reply): nothing here may raise."""
    client = queue.get_client()
    stream = jobs.requests_stream_key()
    await streams.ensure_consumer_group(client)
    for sequence in range(350):
        await client.xadd(stream, {"payload": "{}"}, id=f"1000-{sequence}")  # 1970: far older than any age
    await client.xreadgroup(jobs.CONSUMER_GROUP, "dead-worker", {stream: ">"}, count=10)  # the oldest ten are pending, never acknowledged
    await queue.publish_request(client, request_id="req-new", text="hi", thread_id="thread-new", ctx=TEST_CTX)

    await streams.trim_stale_entries(client, stream, max_age_seconds=3_600)
    claimed = await streams.reclaim_stale_entries(client, stream=stream, group=jobs.CONSUMER_GROUP, consumer="rescuer", min_idle_ms=0)

    remaining = [entry_id for entry_id, _fields in await client.xrange(stream)]
    assert len(remaining) < 350 - 100, "whole nodes of old entries are gone"
    assert remaining[-1].split("-")[0] != "1000", "the recent job is still there"
    assert claimed == []
    assert (await client.xpending(stream, jobs.CONSUMER_GROUP))["pending"] == 0


async def test_a_consumer_that_owns_nothing_is_removed_when_its_worker_stops_and_one_that_owns_a_job_is_not():
    client = queue.get_client()
    stream = jobs.requests_stream_key()
    await streams.ensure_consumer_group(client)
    await queue.publish_request(client, request_id="req-a", text="hi", thread_id="thread-a", ctx=TEST_CTX)
    await queue.publish_request(client, request_id="req-b", text="hi", thread_id="thread-b", ctx=TEST_CTX)
    [(_, [(done_id, _f)])] = await client.xreadgroup(jobs.CONSUMER_GROUP, "finished-worker", {stream: ">"}, count=1)
    await client.xreadgroup(jobs.CONSUMER_GROUP, "busy-worker", {stream: ">"}, count=1)
    await streams.ack_and_delete(client, stream, jobs.CONSUMER_GROUP, done_id)

    await streams.release_consumer(client, stream, jobs.CONSUMER_GROUP, "finished-worker")
    await streams.release_consumer(client, stream, jobs.CONSUMER_GROUP, "busy-worker")

    names = {info["name"] for info in await client.xinfo_consumers(stream, jobs.CONSUMER_GROUP)}
    assert names == {"busy-worker"}


async def test_a_published_event_and_its_expiry_land_together_on_a_real_server():
    """`XADD` and `EXPIRE` are one MULTI now: the stream exists with a lifetime the moment it holds its first event (PRODUCTION_REVIEW.md R08)."""
    client = queue.get_client()
    request_id = str(uuid.uuid4())

    await streams.publish_result(client, request_id, {"type": "token", "content": "hi"})
    await streams.publish_result(client, request_id, {"type": "done"})

    key = jobs.results_stream_key(request_id)
    assert 0 < await client.ttl(key) <= jobs.RESULTS_STREAM_TTL_SECONDS
    assert [event async for event in queue.read_results(client, request_id)] == [{"type": "token", "content": "hi"}, {"type": "done"}]


async def test_the_queue_gauges_read_a_real_servers_lag_and_pending_and_ack_and_delete_does_not_make_it_unknowable():
    """`record_queue_depth` reads XINFO GROUPS' `lag`. Redis reports none (null) once entries were deleted from the middle of a group's UNREAD range, and the
    workers now delete every job they acknowledge: this shows that deleting delivered entries from the front keeps `lag` known (PRODUCTION_REVIEW.md R17)."""
    from app.core import metrics

    client = queue.get_client()
    stream = jobs.requests_stream_key()
    await streams.ensure_consumer_group(client)
    for n in range(8):
        await queue.publish_request(client, request_id=f"r{n}", text="hi", thread_id=f"t{n}", ctx=TEST_CTX)
    delivered = await client.xreadgroup(jobs.CONSUMER_GROUP, "w1", {stream: ">"}, count=3)
    ids = [entry_id for entry_id, _ in delivered[0][1]]

    [row] = [g for g in await client.xinfo_groups(stream) if g["name"] == jobs.CONSUMER_GROUP]
    assert (row["pending"], row["lag"]) == (3, 5)

    await streams.ack_and_delete(client, stream, jobs.CONSUMER_GROUP, ids[0])  # a worker finished its first job
    await streams.ack_and_delete(client, stream, jobs.CONSUMER_GROUP, ids[1])
    [row] = [g for g in await client.xinfo_groups(stream) if g["name"] == jobs.CONSUMER_GROUP]
    assert (row["pending"], row["lag"]) == (1, 5), "lag still known after delivered entries were deleted"

    await streams.record_queue_depth(client, stream, jobs.CONSUMER_GROUP, "integration-test")
    assert metrics.agent_queue_lag is not None  # set without raising against the real reply shape
