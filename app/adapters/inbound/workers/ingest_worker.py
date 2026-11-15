"""Redis Streams consumer: the worker half of the ingestion pipeline. Consumer-group delivery hands each job on
`ingest_queue.py::INGEST_REQUESTS_STREAM` to exactly one worker. Same shape as
`app/adapters/inbound/workers/agent_worker.py`, deliberately on a separate queue/group (see `ingest_queue.py`).

Per job: download from MinIO (`object_store.py`), dispatch to a PDF/DOCX extractor by extension
(`extractors.py`), then feed the text into the shared chunk/embed/upsert pipeline
(`ingestor.py::ingest_text`). This worker owns only download, dispatch and queue plumbing.

A worker that dies mid-job leaves its entry pending forever unless reclaimed; `_reclaim_loop` (every worker
runs one) does it via `XAUTOCLAIM`. Unlike agent_worker.py, a reclaimed ingest job is always safe to
re-publish (capped by `MAX_AUTO_RECLAIM_RETRIES`): `ingest_text` derives Qdrant point ids from content
(`_content_point_id`), so a re-run upserts onto the same ids instead of duplicating (see
`_handle_reclaimed_job`).

`run()` runs up to `_MAX_CONCURRENCY` jobs at once (semaphore acquired before the task is created, as in
agent_worker.py). A job holds no shared in-process state; the module-level MinIO/Qdrant/embedding clients are
lazy singletons safe under concurrent use.

Download and extraction are blocking (MinIO SDK, pypdf/python-docx), so both go through `asyncio.to_thread` or
one job would stall every other's I/O. That doesn't lift the GIL: extraction is CPU-bound, so `to_thread` only
overlaps I/O with one job's parsing. For parse-heavy loads add worker PROCESSES; raise `_MAX_CONCURRENCY` when
jobs mostly wait on MinIO/embeddings/Qdrant. `ingest_text` and its `on_progress` callback
(`_make_progress_reporter`) are plain `async def`, with no thread bridge.

Run with `python -m app.adapters.inbound.workers.ingest_worker` (Makefile `ingest-worker`); needs Redis + MinIO from `make
up`, is not started by it, and is opt-in alongside `POST /ingest/upload` (app/adapters/inbound/api/routers/ingest.py), which
only publishes the job.
"""
import asyncio
import concurrent.futures
import json
import logging
import signal
import socket
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

import redis.asyncio as redis

from app.adapters.inbound.workers.ingest_streams import (
    ensure_consumer_group,
    publish_result,
)
from app.adapters.inbound.workers.streams import (
    READ_BACKOFF_START_SECONDS,
    RECLAIM_ATTEMPTS_FIELD,
    ack_and_delete,
    claim_free_slots,
    observe_queue_wait,
    pause_after_failed_read,
    publish_dead_letter,
    reclaim_stale_entries,
    record_queue_depth,
    release_consumer,
    release_slots,
    republish_job,
    trim_stale_entries,
)
from app.bootstrap import close_adapters, verify_wired, wire
from app.core import metrics
from app.core.config import (
    INGEST_WORKER_MAX_CONCURRENCY,
    INGEST_WORKER_RECLAIM_IDLE_SECONDS,
    MAX_AUTO_RECLAIM_RETRIES,
    REQUESTS_STREAM_MAX_AGE_SECONDS,
    WORKER_RECLAIM_INTERVAL_SECONDS,
)
from app.ingestion import ingestor
from app.ingestion.extractors import EXTRACTORS_BY_SUFFIX, ExtractionFailedError
from app.platform.logging_config import bind_request_id, configure_logging
from app.platform.profiling import start_profiling
from app.platform.redis_client import UNAVAILABLE_ERRORS, StreamReadResponse, get_client
from app.platform.telemetry import configure_telemetry
from app.platform.worker_heartbeat import beat
from app.ports.outbound.jobs import INGEST_CONSUMER_GROUP, INGEST_REQUESTS_STREAM
from app.ports.outbound.storage import object_store

logger = logging.getLogger(__name__)

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"
_MAX_CONCURRENCY = INGEST_WORKER_MAX_CONCURRENCY  # concurrent jobs per worker process (see module docstring)
_BLOCK_MS = 5000


def _make_progress_reporter(client: redis.Redis, job_id: str) -> Callable[[int, int], Awaitable[None]]:
    """An `async def` callback `ingestor.ingest_text` awaits directly (it
    runs on this worker's own event loop, so no thread-bridging needed).
    Fire-and-forget in spirit, not mechanism: a failed progress publish is
    logged, never raised — the terminal `done`/`error` event published
    below is what actually matters for correctness."""

    async def on_progress(done: int, total: int) -> None:
        try:
            await publish_result(client, job_id, {"type": "progress", "done": done, "total": total})
        except Exception as exc:  # noqa: BLE001 - a dropped progress tick must not fail the ingest
            logger.warning(
                "ingest_worker_progress_publish_failed",
                extra={"job_id": job_id, "error_class": type(exc).__name__},
            )

    return on_progress


async def process_job(client: redis.Redis, entry_id: str, fields: dict[str, Any]) -> None:
    """Run one ingest job and publish its outcome — always ack, even on
    failure, same shape as `agent_worker.py::process_request`. Not because
    a retry would duplicate anything (`ingestor.ingest_text`'s point ids
    are content-derived now, so re-running is safe — see
    `_handle_reclaimed_job`'s own docstring, which DOES retry on this
    same basis): a failure here (a corrupt file, a bad extraction) is
    usually deterministic, so blindly retrying within this same attempt
    would just fail again — surfacing the error immediately and letting
    the caller decide whether to re-upload is the more useful behavior."""
    payload = json.loads(fields["payload"])
    job_id = payload["job_id"]
    observe_queue_wait("ingest", payload)  # how long this job waited for a worker (R17)
    with bind_request_id(job_id):
        try:
            await publish_result(client, job_id, {"type": "started"})
            filename = payload["filename"]
            suffix = Path(filename).suffix.lower()
            extractor = EXTRACTORS_BY_SUFFIX.get(suffix)
            if extractor is None:
                raise ExtractionFailedError(
                    f"unsupported file type {suffix!r} — only "
                    f"{sorted(EXTRACTORS_BY_SUFFIX)} are supported"
                )
            # Both offloaded via asyncio.to_thread — see module docstring:
            # a blocking call on the event loop would stall every OTHER
            # in-flight job's I/O.
            data = await asyncio.to_thread(object_store.get().download_bytes, payload["object_key"])
            text = await asyncio.to_thread(extractor, data)
            on_progress = _make_progress_reporter(client, job_id)
            chunks = await ingestor.ingest_text(
                text,
                title=Path(filename).stem,
                ctx=payload["ctx"],
                source=f"upload:{filename}",
                topic=payload.get("topic"),
                on_progress=on_progress,
            )
            await publish_result(client, job_id, {"type": "done", "chunks": chunks})
        except Exception as exc:  # noqa: BLE001 - the queue must keep moving regardless
            # Log the actual message, not just error_class (same truncated-
            # string convention as sandbox_session.py/skills.py) — otherwise
            # the failure reason only lives in the results stream, which
            # expires after RESULTS_STREAM_TTL_SECONDS.
            logger.warning(
                "ingest_worker_job_failed",
                extra={
                    "job_id": job_id,
                    "error_class": type(exc).__name__,
                    "error": str(exc)[:300],
                },
            )
            await publish_result(client, job_id, {"type": "error", "content": str(exc)})
        finally:
            await ack_and_delete(client, INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, entry_id)


async def _process_with_limit(
    client: redis.Redis, entry_id: str, fields: dict[str, Any], semaphore: asyncio.Semaphore
) -> None:
    """Runs one job under `semaphore` and releases it when done, success or
    failure — same shape as `agent_worker.py::_process_with_limit`;
    `process_job` already acks regardless of outcome, so this wrapper only
    owns the concurrency slot."""
    try:
        await process_job(client, entry_id, fields)
    finally:
        semaphore.release()


async def _handle_reclaimed_job(client: redis.Redis, entry_id: str, fields: dict[str, Any]) -> None:
    """One entry `_reclaim_loop` found idle past
    `INGEST_WORKER_RECLAIM_IDLE_SECONDS` — its original worker almost
    certainly died mid-job (crashed, OOM-killed) before ever reaching
    `process_job`'s own ack. Always safe to silently republish now
    (`streams.py::republish_job`, capped by `MAX_AUTO_RECLAIM_RETRIES`, same
    poison-pill protection as `agent_worker.py`'s own version): every point
    `ingestor.ingest_text` writes has an id derived entirely from its own
    content (`_content_point_id`), never a random draw, so re-running the
    SAME job from scratch upserts onto the exact same ids instead of
    duplicating whatever the crashed attempt already wrote — unlike a
    reclaimed agent "turn", there's no equivalent to a mutating TOOL call
    to check for here; the job's only side effect (the Qdrant upsert) IS
    the idempotent operation. Only once `MAX_AUTO_RECLAIM_RETRIES` is
    exhausted (a job that keeps crashing whichever worker picks it up) does
    this fall back to surfacing an error and archiving to a dead-letter
    stream, same as before this guarantee existed."""
    try:
        payload = json.loads(fields["payload"])
    except (KeyError, json.JSONDecodeError) as exc:
        logger.warning(
            "ingest_worker_reclaimed_payload_unreadable",
            extra={"entry_id": entry_id, "error_class": type(exc).__name__},
        )
        await _dead_letter_reclaimed_ingest_job(client, entry_id, {}, reason="unreadable_payload")
        return

    job_id = payload.get("job_id")
    attempts = payload.get(RECLAIM_ATTEMPTS_FIELD, 0)
    logger.warning(
        "ingest_worker_job_reclaimed",
        # NOT "filename" — that key collides with LogRecord's own built-in
        # attribute of the same name and raises KeyError from inside
        # logging's makeRecord.
        extra={
            "entry_id": entry_id,
            "job_id": job_id,
            "upload_filename": payload.get("filename"),
            "attempts": attempts,
        },
    )

    if attempts < MAX_AUTO_RECLAIM_RETRIES:
        await republish_job(client, requests_stream=INGEST_REQUESTS_STREAM, payload=payload)
        metrics.agent_worker_job_reclaimed_total.labels(queue="ingest", outcome="retried").inc()
        await ack_and_delete(client, INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, entry_id)
        return

    if job_id:
        await publish_result(
            client,
            job_id,
            {
                "type": "error",
                "content": (
                    "The worker processing this upload kept failing after "
                    "multiple attempts. Please upload it again."
                ),
            },
        )
    await _dead_letter_reclaimed_ingest_job(client, entry_id, payload, reason="worker_lost")


async def _dead_letter_reclaimed_ingest_job(client: redis.Redis, entry_id: str, payload: dict[str, Any], *, reason: str) -> None:
    metrics.agent_worker_job_reclaimed_total.labels(queue="ingest", outcome="dead_lettered").inc()
    await publish_dead_letter(
        client, requests_stream=INGEST_REQUESTS_STREAM, entry_id=entry_id, payload=payload, reason=reason
    )
    await ack_and_delete(client, INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, entry_id)


async def _reclaim_loop(client: redis.Redis, stop_event: asyncio.Event) -> None:
    """Same shape and reasoning as `agent_worker.py::_reclaim_loop` — see
    there for why this doesn't just redeliver a reclaimed job, and why a
    transient Redis error here is logged and retried next interval rather
    than ending the loop."""
    min_idle_ms = INGEST_WORKER_RECLAIM_IDLE_SECONDS * 1000
    while not stop_event.is_set():
        beat("ingest", "reclaim")  # proof of life for the container's health check (app/platform/worker_heartbeat.py)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=WORKER_RECLAIM_INTERVAL_SECONDS)
            return  # stop_event was set during the wait — shutting down
        except TimeoutError:
            pass  # normal case: interval elapsed, run a pass below
        try:
            entries = await reclaim_stale_entries(
                client,
                stream=INGEST_REQUESTS_STREAM,
                group=INGEST_CONSUMER_GROUP,
                consumer=CONSUMER_NAME,
                min_idle_ms=min_idle_ms,
            )
            for entry_id, fields in entries:
                await _handle_reclaimed_job(client, entry_id, fields)
            await trim_stale_entries(client, INGEST_REQUESTS_STREAM, max_age_seconds=REQUESTS_STREAM_MAX_AGE_SECONDS)
            await record_queue_depth(client, INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, "ingest")
        except Exception as exc:  # noqa: BLE001 - one bad pass must not end reclaim for this process's whole life
            logger.warning("ingest_worker_reclaim_pass_failed", extra={"error_class": type(exc).__name__})


async def run() -> None:
    wire()
    verify_wired()
    loop = asyncio.get_running_loop()
    # asyncio.to_thread borrows the loop's DEFAULT executor
    # (min(32, os.cpu_count()+4)) — capped at 32 regardless of host, which
    # would silently throttle concurrency if INGEST_WORKER_MAX_CONCURRENCY
    # is raised past that. Sized explicitly to _MAX_CONCURRENCY instead: a
    # job holds at most one pool thread at a time, so N concurrent jobs need
    # at most N threads. Not padded by CPU count — the GIL, not thread
    # count, bounds CPU-bound throughput (see module docstring), and
    # _MAX_CONCURRENCY is already the operator's tuning knob
    # (WORKER_CONCURRENCY.md).
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=_MAX_CONCURRENCY, thread_name_prefix="ingest-worker"
        )
    )

    client = get_client()
    await ensure_consumer_group(client)
    logger.info(
        "ingest_worker_started",
        extra={"consumer": CONSUMER_NAME, "max_concurrency": _MAX_CONCURRENCY},
    )

    # Graceful shutdown — same reasoning as agent_worker.py's run(): SIGTERM/
    # SIGINT stops this worker from claiming a NEW job but never interrupts
    # one in flight (a redelivered job would re-embed/re-upsert the same
    # chunks — see process_job's docstring). A worker that dies WITHOUT this
    # graceful path still abandons its claimed entry — _reclaim_loop below,
    # run by every replica in the pool, recovers from that case.
    stop_event = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    reclaim_task = asyncio.create_task(_reclaim_loop(client, stop_event))

    # Bounds jobs this process runs at once. The slots are taken BEFORE the read and the read asks for no more entries than it holds, so a full worker
    # reads nothing: entries stay pending for the group instead of piling up unbounded in `in_flight`, and a claimed entry's idle clock never runs while
    # it waits for a slot (pattern 99). Same shape as agent_worker.py::run(), which also says why a failed read is retried.
    semaphore = asyncio.Semaphore(_MAX_CONCURRENCY)
    in_flight: set[asyncio.Task[Any]] = set()

    read_backoff = READ_BACKOFF_START_SECONDS
    while not stop_event.is_set():
        beat("ingest", "read")
        free = await claim_free_slots(
            semaphore, stop_event, limit=_MAX_CONCURRENCY, wait_seconds=_BLOCK_MS / 1000, on_wait=lambda: beat("ingest", "read")
        )
        if not free:
            continue  # told to stop while every slot was busy
        try:
            response = cast(
                StreamReadResponse,
                await client.xreadgroup(
                    INGEST_CONSUMER_GROUP,
                    CONSUMER_NAME,
                    {INGEST_REQUESTS_STREAM: ">"},
                    count=free,
                    block=_BLOCK_MS,
                ),
            )
        except UNAVAILABLE_ERRORS as exc:
            release_slots(semaphore, free)
            metrics.agent_worker_queue_read_failed_total.labels(queue="ingest").inc()
            logger.warning("ingest_worker_queue_read_failed", extra={"queue": "ingest", "error_class": type(exc).__name__})
            read_backoff = await pause_after_failed_read(stop_event, read_backoff)
            continue
        read_backoff = READ_BACKOFF_START_SECONDS
        entries = response[0][1] if response else []
        for entry_id, fields in entries:
            task = asyncio.create_task(_process_with_limit(client, entry_id, fields, semaphore))  # the slot it holds is its own to release
            in_flight.add(task)
            task.add_done_callback(in_flight.discard)
        release_slots(semaphore, free - len(entries))  # what the read did not use

    logger.info(
        "ingest_worker_stopping", extra={"consumer": CONSUMER_NAME, "in_flight": len(in_flight)}
    )
    # Let every already-claimed job finish (and ack) instead of abandoning it
    # mid-job — see the graceful-shutdown comment above.
    if in_flight:
        await asyncio.gather(*in_flight)
    await reclaim_task  # stop_event already set above, so this returns promptly
    await release_consumer(client, INGEST_REQUESTS_STREAM, INGEST_CONSUMER_GROUP, CONSUMER_NAME)  # see agent_worker.py's shutdown
    await client.aclose()
    # The Qdrant client the ingestor wrote through (same reasoning as agent_worker.py's shutdown).
    await close_adapters()


if __name__ == "__main__":
    configure_logging()
    configure_telemetry("agent-core-ingest-worker")
    start_profiling("agent-core-ingest-worker")
    asyncio.run(run())
