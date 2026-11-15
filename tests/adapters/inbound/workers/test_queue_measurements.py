"""The three numbers that size the worker pool (PRODUCTION_REVIEW.md R17): how long a job waited, how many are waiting and how many are running.

There were fifty-five alert rules and none could say "the queue is backing up": no metric reported how many jobs were waiting or how long one waited
before a worker took it, and `WORKER_CONCURRENCY.md` says nothing scales on queue depth while nothing could even see it.
"""
import asyncio
import json
import time

import pytest

from app.adapters.inbound.workers import agent_worker, ingest_worker, streams
from app.ports.outbound import jobs
from tests.adapters.outbound.redis.test_queue import FakeRedis
from tests.conftest import _METRIC_READER


def _histogram(name, **labels):
    """(count, sum) of a histogram's recorded points for `labels`."""
    data = _METRIC_READER.get_metrics_data()
    count, total = 0, 0.0
    for resource in (data.resource_metrics if data else []):
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    if all(point.attributes.get(k) == v for k, v in labels.items()):
                        count, total = point.count, point.sum
    return count, total


def _gauges(domain):
    """`{metric name: value}` of the queue gauges for `domain`, from ONE collection: a synchronous OTel gauge reports a value once after it is set, so
    reading two of them with two collections would find the second gone."""
    found = {}
    data = _METRIC_READER.get_metrics_data()
    for resource in (data.resource_metrics if data else []):
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name in {"agent_queue_pending", "agent_queue_lag"}:
                    for point in metric.data.data_points:
                        if point.attributes.get("domain") == domain:
                            found[metric.name] = point.value
    return found


class TestQueueWait:
    def test_a_stamped_job_observes_how_long_it_waited(self):
        before_count, before_sum = _histogram("agent_queue_wait_seconds", queue="agent")

        streams.observe_queue_wait("agent", {"enqueued_at": time.time() - 4.0})

        count, total = _histogram("agent_queue_wait_seconds", queue="agent")
        assert count == before_count + 1 and 3.5 < total - before_sum < 6.0

    @pytest.mark.parametrize("payload", [{}, {"enqueued_at": None}, {"enqueued_at": "yesterday"}, {"enqueued_at": True}, {"enqueued_at": [1]}])
    def test_a_job_with_no_usable_stamp_is_skipped_not_counted_as_zero(self, payload):
        """A job published by an older API during a rolling deploy has none."""
        before = _histogram("agent_queue_wait_seconds", queue="agent")[0]

        streams.observe_queue_wait("agent", payload)

        assert _histogram("agent_queue_wait_seconds", queue="agent")[0] == before

    def test_a_clock_a_little_behind_the_publishers_is_no_wait_not_a_negative_one(self):
        before_sum = _histogram("agent_queue_wait_seconds", queue="agent")[1]

        streams.observe_queue_wait("agent", {"enqueued_at": time.time() + 5.0})

        assert _histogram("agent_queue_wait_seconds", queue="agent")[1] - before_sum < 0.01

    async def test_the_agent_worker_observes_the_wait_when_it_takes_a_job(self, monkeypatch):
        async def turn(text, thread_id, ctx, **kwargs):
            yield {"type": "done"}

        monkeypatch.setattr(agent_worker, "astream_events_turn", turn)
        client = FakeRedis()
        payload = {"kind": "turn", "request_id": "r", "text": "hi", "thread_id": "t", "ctx": {"tenant": "a", "principal": "p", "claims": {}}, "enqueued_at": time.time() - 2.0}
        before = _histogram("agent_queue_wait_seconds", queue="agent")[0]

        await agent_worker.process_request(client, "0-1", {"payload": json.dumps(payload)}, domain="ecorp")

        assert _histogram("agent_queue_wait_seconds", queue="agent")[0] == before + 1

    async def test_the_ingest_worker_observes_it_too_under_its_own_label(self, monkeypatch):
        client = FakeRedis()
        payload = {"job_id": "j", "object_key": "k", "filename": "x.unsupported", "content_type": "x", "ctx": {"tenant": "a", "principal": "p", "claims": {}}, "enqueued_at": time.time() - 1.0}
        before = _histogram("agent_queue_wait_seconds", queue="ingest")[0]

        await ingest_worker.process_job(client, "0-1", {"payload": json.dumps(payload)})

        assert _histogram("agent_queue_wait_seconds", queue="ingest")[0] == before + 1

    async def test_a_republished_job_keeps_its_original_stamp(self):
        """A crash shows up as a long wait: the retry's clock does not restart."""
        client = FakeRedis()
        payload = {"request_id": "r", "enqueued_at": 1234.5}

        await streams.republish_job(client, requests_stream="agent:requests:ecorp", payload=payload)

        republished = json.loads(client.streams["agent:requests:ecorp"][0][1]["payload"])
        assert republished["enqueued_at"] == 1234.5


class TestQueueDepth:
    async def _client(self, waiting, running):
        client = FakeRedis()
        stream = jobs.requests_stream_key("ecorp")
        await streams.ensure_consumer_group(client, "ecorp")
        for _ in range(waiting + running):
            await client.xadd(stream, {"payload": "{}"})
        for _ in range(running):
            await client.xreadgroup(jobs.CONSUMER_GROUP, "w1", {stream: ">"}, count=1)
        return client, stream

    async def test_pending_and_lag_are_set_for_the_domain(self):
        client, stream = await self._client(waiting=7, running=3)

        await streams.record_queue_depth(client, stream, jobs.CONSUMER_GROUP, "ecorp")

        assert _gauges("ecorp") == {"agent_queue_pending": 3, "agent_queue_lag": 7}

    async def test_a_lag_redis_cannot_report_is_left_alone_rather_than_set_to_zero(self):
        client, stream = await self._client(waiting=9, running=1)
        await streams.record_queue_depth(client, stream, jobs.CONSUMER_GROUP, "ops")
        assert _gauges("ops")["agent_queue_lag"] == 9
        client.lag_unknown = True

        await streams.record_queue_depth(client, stream, jobs.CONSUMER_GROUP, "ops")

        assert "agent_queue_lag" not in _gauges("ops"), "nothing is set: a zero would read as an empty queue"

    async def test_a_stream_with_no_such_group_sets_nothing(self):
        await streams.record_queue_depth(FakeRedis(), "agent:requests:nowhere", jobs.CONSUMER_GROUP, "nowhere")

        assert _gauges("nowhere") == {}

    async def test_every_reclaim_pass_of_each_worker_sets_its_own_domains_gauges(self, monkeypatch):
        monkeypatch.setattr(agent_worker, "WORKER_RECLAIM_INTERVAL_SECONDS", 0.01)
        client, _stream = await self._client(waiting=4, running=0)
        stop = asyncio.Event()

        task = asyncio.create_task(agent_worker._reclaim_loop(client, stop, graphs={"ecorp": object()}))
        await asyncio.sleep(0.08)
        stop.set()
        await asyncio.wait_for(task, 1)

        assert _gauges("ecorp")["agent_queue_lag"] == 4

    async def test_the_ingest_workers_pass_does_the_same_under_the_ingest_label(self, monkeypatch):
        monkeypatch.setattr(ingest_worker, "WORKER_RECLAIM_INTERVAL_SECONDS", 0.01)
        client = FakeRedis()
        await client.xgroup_create(jobs.INGEST_REQUESTS_STREAM, jobs.INGEST_CONSUMER_GROUP, id="0", mkstream=True)
        for _ in range(2):
            await client.xadd(jobs.INGEST_REQUESTS_STREAM, {"payload": "{}"})
        stop = asyncio.Event()

        task = asyncio.create_task(ingest_worker._reclaim_loop(client, stop))
        await asyncio.sleep(0.08)
        stop.set()
        await asyncio.wait_for(task, 1)

        assert _gauges("ingest")["agent_queue_lag"] == 2
