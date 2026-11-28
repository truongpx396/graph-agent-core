"""A pool of workers that answers nothing must be visible to an alert.

`read_results` bounds the wait for the FIRST event of a job (`first_event_deadline_
seconds`) and, on expiry, yields an `error` event to whoever is reading. That turns
a hang into an error for ONE caller — but nothing recorded it, so a total outage (no
agent-worker running for a domain, or no ingest-worker at all) produced a stream of
identical per-request errors and no signal an operator could be paged on. Every
request failed and no alert could fire (spec 008, B21: read from the code, no metric
at all on that path).

`agent_worker_unreachable_total{queue}` is the signal, and `WorkerUnreachable` in
observability/prometheus/alerts.yml is the rule on it
(tests/core/test_alert_rules.py checks the rule exists and watches a real metric).
Only the expiry counts: a job that was picked up, however slow, is not "unreachable".
"""
import asyncio

import pytest

from app.adapters.inbound.workers import ingest_streams, streams
from app.adapters.outbound.redis import ingest_queue, queue
from app.core import metrics
from app.ports.outbound import jobs
from tests.adapters.outbound.redis.test_queue import FakeRedis
from tests.conftest import TEST_CTX, metric_value


def _unreachable(queue_name: str) -> float:
    return metric_value(metrics.agent_worker_unreachable_total, queue=queue_name)


async def _drain(generator):
    return [event async for event in generator]


async def test_an_agent_job_nobody_picks_up_counts_as_an_unreachable_worker():
    before = _unreachable("agent")

    events = await _drain(queue.read_results(FakeRedis(), "r1", first_event_deadline_seconds=0.05))

    assert [e["type"] for e in events] == ["error"]
    assert _unreachable("agent") - before == 1


class _BlockingRedis(FakeRedis):
    """An empty XREAD waits out its block, as a server's does, so a worker's task gets to run while the reader waits."""

    async def xread(self, streams, block=None, count=None):
        result = await super().xread(streams, block=block, count=count)
        if not result:
            await asyncio.sleep((block or 0) / 1000)
        return result


async def _published_job(client):
    await queue.publish_request(client, request_id="r1", text="remember this", thread_id="t1", ctx=TEST_CTX)


async def _read(client):
    """What a worker (or the reader giving up on one) said: the producer's own "queued" status is left out."""
    events = await _drain(queue.read_results(client, "r1", block_ms=10, first_event_deadline_seconds=0.05))
    return [event for event in events if not jobs.is_queued_status(event)]


async def test_the_queued_status_is_not_a_worker_answering():
    """The producer writes "queued" with the job, so the stream is never empty: a deadline that any event cleared would wait for ever."""
    client = _BlockingRedis()
    await _published_job(client)
    before = _unreachable("agent")

    events = await asyncio.wait_for(_drain(queue.read_results(client, "r1", block_ms=10, first_event_deadline_seconds=0.05)), 5)

    assert [(e["type"], e.get("state") or e.get("code")) for e in events] == [("status", "queued"), ("error", "worker_unreachable")]
    assert _unreachable("agent") - before == 1


async def test_a_started_job_is_waited_for_however_long_it_is_silent():
    client = _BlockingRedis()
    await _published_job(client)
    await streams.publish_result(client, "r1", jobs.status_event("started", worker="w-1"))  # and no lock: only the status can stop the deadline

    reader = asyncio.create_task(_read(client))
    await asyncio.sleep(0.2)
    assert not reader.done()
    await streams.publish_result(client, "r1", {"type": "done"})

    assert [e["type"] for e in await asyncio.wait_for(reader, 5)] == ["status", "done"]


async def test_a_reader_that_only_saw_queued_is_still_waiting_for_a_worker():
    """Until "queued", a client held a position only after a worker had written, so a read from a position had no deadline. A reconnect after
    "queued" with no worker would then wait for ever."""
    client = _BlockingRedis()
    await _published_job(client)
    queued_id = client.streams[jobs.results_stream_key("r1")][0][0]

    reader = queue.read_results_from(client, "r1", after=queued_id, block_ms=10, first_event_deadline_seconds=0.05)

    assert [event["code"] for _, event in await asyncio.wait_for(_drain(reader), 5)] == ["worker_unreachable"]


async def test_a_job_a_worker_is_still_running_is_not_given_up_on():
    """Trace 7e6cad06: the model took 28.6 s to write a tool call, which streams no tokens, so the turn's first event came 0.3 s after the
    30 s deadline. The person saw "is an agent-worker running?" and never the approval box, though the worker had paused for it."""
    client = _BlockingRedis()
    await _published_job(client)
    await streams.acquire_thread_lock(client, "t1", "worker")  # what a worker does before it runs a job
    before = _unreachable("agent")

    reader = asyncio.create_task(_read(client))
    await asyncio.sleep(0.2)  # several deadlines pass with nothing published
    assert not reader.done()
    await streams.publish_result(client, "r1", {"type": "approval_required", "tool_calls": []})

    assert [e["type"] for e in await asyncio.wait_for(reader, 5)] == ["approval_required"]
    assert _unreachable("agent") == before


async def test_a_worker_that_dies_before_publishing_is_still_reported():
    client = _BlockingRedis()
    await _published_job(client)
    await streams.acquire_thread_lock(client, "t1", "worker")

    reader = asyncio.create_task(_read(client))
    await asyncio.sleep(0.1)
    await streams.release_thread_lock(client, "t1", "worker")  # a crashed worker's lock expires the same way

    assert [e["type"] for e in await asyncio.wait_for(reader, 5)] == ["error"]


async def test_an_event_published_just_after_the_lock_was_released_is_not_lost():
    """The worker releases the lock and then publishes its terminal event, so the reader looks once more before it gives up."""

    class _PublishesAtTheLastLook(_BlockingRedis):
        async def xread(self, streams_, block=None, count=None):
            if block == queue.LAST_LOOK_MS:
                await streams.publish_result(self, "r1", {"type": "done"})
            return await super().xread(streams_, block=block, count=count)

    client = _PublishesAtTheLastLook()
    await _published_job(client)

    assert [e["type"] for e in await asyncio.wait_for(_read(client), 5)] == ["done"]


async def test_an_ingest_job_nobody_picks_up_counts_as_an_unreachable_worker():
    before = _unreachable("ingest")

    events = await _drain(ingest_queue.read_results(FakeRedis(), "j1", first_event_deadline_seconds=0.05))

    assert [e["type"] for e in events] == ["error"]
    assert _unreachable("ingest") - before == 1


async def test_each_queue_counts_under_its_own_label():
    agent_before, ingest_before = _unreachable("agent"), _unreachable("ingest")

    await _drain(queue.read_results(FakeRedis(), "r1", first_event_deadline_seconds=0.05))

    assert _unreachable("ingest") == ingest_before and _unreachable("agent") - agent_before == 1


@pytest.mark.parametrize(
    ("module", "publish", "kind"),
    [(queue, streams.publish_result, "agent"), (ingest_queue, ingest_streams.publish_result, "ingest")],
    ids=["agent", "ingest"],
)
async def test_a_job_that_was_picked_up_is_never_counted_however_the_deadline_compares(module, publish, kind):
    """The deadline guards the wait for the first event only; a worker that
    answered is, by definition, reachable."""
    client = FakeRedis()
    await publish(client, "r1", {"type": "token", "content": "a"})
    await publish(client, "r1", {"type": "done"})
    before = _unreachable(kind)

    await _drain(module.read_results(client, "r1", first_event_deadline_seconds=0.001))

    assert _unreachable(kind) == before


async def test_a_read_with_no_deadline_never_counts_anything():
    client = FakeRedis()
    await streams.publish_result(client, "r1", {"type": "done"})
    before = _unreachable("agent")

    await _drain(queue.read_results(client, "r1"))

    assert _unreachable("agent") == before
