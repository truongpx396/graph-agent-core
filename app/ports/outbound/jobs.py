"""The agent's work queue as the API sees it, and the names the API and the workers must agree on.

A turn is a job on a per-domain requests stream; its events come back on a per-request results stream. `TurnQueue` is what the API does with that:
submit a turn, a resume or a cancel, flag a running turn to stop, read the events back. `IngestQueue` is the same for an upload.

Reading the requests stream as a worker (the consumer group, acknowledging, reclaiming a dead worker's jobs, the dead-letter stream) is the workers'
own Streams technology, not a port: nothing in the application calls it (`app/adapters/inbound/workers/streams.py`). What both halves must agree on is
plain data and lives here: stream and key names, TTLs, which events end a stream. The Redis adapters are `app/adapters/outbound/redis/queue.py` and
`ingest_queue.py`.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.config import (
    THREAD_LOCK_TTL_SECONDS as THREAD_LOCK_TTL_SECONDS,  # re-exported: the API and the workers read the names they share from here
)
from app.core.security import SecurityCtx
from app.ports.slot import Slot

CONSUMER_GROUP = "agent-workers"
# The last event type a turn ever publishes: the reader stops at one, and the worker releases the thread lock the moment it publishes one.
TERMINAL_EVENT_TYPES = frozenset({"done", "error", "approval_required"})
RESULTS_STREAM_TTL_SECONDS = 300  # an abandoned results stream, chat or ingest, expires after 5 minutes
CANCEL_FLAG_TTL_SECONDS = 60  # outlasts the gap between a worker's cancel checks; short, so a flag nobody consumed cannot cancel a later turn
# THREAD_LOCK_TTL_SECONDS (imported above, defined in app/core/config.py beside the validator that needs it): how long a conversation's lock lives
# without being renewed. The worker renews it while the job runs, so it only bounds how long a CRASHED worker's lock keeps the conversation busy.

INGEST_REQUESTS_STREAM = "ingest:requests"
INGEST_CONSUMER_GROUP = "ingest-workers"


def status_event(state: str, *, worker: str | None = None) -> dict[str, Any]:
    """Where a turn's job is, as the client is told (`StatusEvent`, not terminal): "queued", written by the producer with the job, and "started",
    written by the worker that takes it. `worker` is an opaque label, never a host name: a caller reads it."""
    event: dict[str, Any] = {"type": "status", "state": state, "at": datetime.now(UTC).isoformat(timespec="milliseconds")}
    if worker is not None:
        event["worker"] = worker
    return event


def is_queued_status(event: dict[str, Any]) -> bool:
    """The one event on a results stream that no worker wrote."""
    return event.get("type") == "status" and event.get("state") == "queued"


def results_stream_key(request_id: str) -> str:
    return f"agent:results:{request_id}"


def requests_stream_key(domain: str = "ecorp") -> str:
    """The one requests stream the workers serving `domain` (`AGENT_WORKER_DOMAINS`) all
    consume from as a single consumer group — see module docstring for why
    domain-scoping keeps each domain's load independent. Defaults to
    `"ecorp"`, same convention as `app/agent/runtime/core.py::DEFAULT_DOMAIN`."""
    return f"agent:requests:{domain}"


def request_thread_key(request_id: str) -> str:
    """Which conversation a request id belongs to, kept for the life of its results stream (the publisher sets it, every event the worker publishes refreshes
    it). It is what lets `GET /chat/stream/{request_id}` check that the caller owns the conversation before it reads another person's events: the results
    stream itself carries no owner (PRODUCTION_REVIEW.md R13)."""
    return f"agent:request_thread:{request_id}"


def cancel_flag_key(thread_id: str) -> str:
    return f"agent:cancel:{thread_id}"


def thread_lock_key(thread_id: str) -> str:
    return f"agent:lock:{thread_id}"


def ingest_results_stream_key(job_id: str) -> str:
    return f"ingest:results:{job_id}"


class TurnQueue(Protocol):
    async def claim_or_get_existing_submission(self, *, thread_id: str, digest: str, request_id: str, ttl_seconds: int) -> tuple[str, bool]:
        """Claims `digest` for `thread_id` for `ttl_seconds`. Returns `(request_id to use, is_new)`: an identical submission inside the window gets the
        first attempt's id and must not publish a second job, so a client's retry streams the same turn instead of running it twice."""
        ...

    async def release_submission_claim(self, *, thread_id: str, digest: str) -> None:
        """Gives back a claim whose job then failed to publish. Best effort: never raises."""
        ...

    async def publish_request(
        self,
        *,
        request_id: str,
        text: str,
        thread_id: str,
        ctx: SecurityCtx,
        domain: str = "ecorp",
        require_approval: bool = False,
        images: list[str] | None = None,
        unattended: bool = False,
    ) -> None:
        """Enqueues a new turn on `domain`'s requests stream; only a worker serving that domain reads it. The caller makes `request_id`, so it can listen
        before publishing. `unattended` is a property of the job and survives a reclaim: nobody is there to approve. The results stream starts
        with a "queued" status, written together with the job."""
        ...

    async def publish_resume_request(self, *, request_id: str, thread_id: str, approved: bool, ctx: SecurityCtx, domain: str = "ecorp") -> None:
        """Enqueues the approve-or-reject decision for a turn paused at human approval. `domain` must be the original turn's. Says "queued" as a
        turn does; a cancel does not, being answered by its outcome."""
        ...

    async def publish_cancel_request(self, *, request_id: str, thread_id: str, ctx: SecurityCtx, domain: str = "ecorp") -> None:
        """Enqueues a cancel for a turn paused at human approval (an actively streaming turn is stopped by `set_cancel_flag`)."""
        ...

    async def set_cancel_flag(self, thread_id: str) -> None:
        """Flags the turn streaming on `thread_id` to stop at its next check. Short-lived."""
        ...

    async def is_thread_busy(self, thread_id: str) -> bool:
        """Whether a worker holds `thread_id`'s conversation lock, that is, a turn is running on it. A conversation paused at an approval is not busy (the worker
        releases the lock when it publishes the pause). A lock that outlived a crashed worker reads as busy until it expires (`THREAD_LOCK_TTL_SECONDS`)."""
        ...

    def read_results(self, request_id: str, *, first_event_deadline_seconds: float | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yields each event of `request_id`'s turn, in order, until a terminal one. `first_event_deadline_seconds` bounds only the wait for a
        WORKER's first event (the "queued" status is the producer's own): if no worker ever publishes, it yields one `error` event and ends
        instead of hanging."""
        ...

    def read_results_from(
        self, request_id: str, *, after: str = "0", first_event_deadline_seconds: float | None = None
    ) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """`read_results` with each event's position: yields `(entry_id, event)` for the events published AFTER `after` (an entry id a client last saw; the
        default is the start), until a terminal one. Reading from a position is what lets a client whose connection dropped pick the turn up where it stopped.
        `first_event_deadline_seconds` applies until a worker has written to the stream, from the start or from a position: a client that saw only
        "queued" is still waiting for a worker, and once one has written, a quiet stretch is a long tool, not "nobody picked it up"."""
        ...

    async def thread_of_request(self, request_id: str) -> str | None:
        """The conversation `request_id` belongs to, or None when the id is unknown or its results have expired (the same lifetime as the stream)."""
        ...

    async def ping(self) -> None:
        """Raises if the queue's server is unreachable (the readiness probe)."""
        ...


class IngestQueue(Protocol):
    async def publish_ingest_request(
        self, *, job_id: str, object_key: str, filename: str, content_type: str, ctx: SecurityCtx, topic: str | None = None
    ) -> None:
        """Enqueues one ingest job. `object_key` points at the uploaded file in the object store; the job never carries the bytes."""
        ...

    def read_results(self, job_id: str, *, first_event_deadline_seconds: float | None = None) -> AsyncIterator[dict[str, Any]]:
        """Yields each event of `job_id`, in order, until `done` or `error`; same first-event deadline as `TurnQueue.read_results`."""
        ...

    async def delete_results_stream(self, job_id: str) -> None:
        """Best-effort cleanup once the reader has seen the end. Never raises."""
        ...


turn_queue: Slot[TurnQueue] = Slot("turn_queue", TurnQueue)
ingest_queue: Slot[IngestQueue] = Slot("ingest_queue", IngestQueue)
