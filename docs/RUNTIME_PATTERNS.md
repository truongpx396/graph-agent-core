# Runtime patterns: the queue, the workers and what they report

How a turn travels from the API to a worker and back, and how the processes stay observable. Numbers are shared with the other pattern documents ([index](README.md#pattern-numbers)). Sizing the pools is in [WORKER_CONCURRENCY.md](WORKER_CONCURRENCY.md).

| # | Lesson |
|---|---|
| 11 | Custom metrics |
| 29 | Built-in web UI |
| 30 | Canonical error envelope |
| 31 | DB connection pool |
| 37 | Per-tool-call audit record |
| 43 | Redis Streams queue between the API and agent workers |
| 49 | Per-domain requests streams |
| 96 | One worker serves many domains |
| 99 | Three timeouts that must stay in order are a validator, not a comment |

### 11. Custom metrics
OpenTelemetry counters and histograms (`app/core/metrics.py`) give the "how often, across everyone" view that Langfuse's per-call traces cannot. Every process pushes OTLP to one collector, so one scrape target covers the API and all workers. Histogram buckets are explicit: the SDK default puts every 1-90 s turn in one bucket.
- **Bug:** scheduled jobs exited before the 15 s export and exported nothing. Each now runs in `job_runtime.py::scheduled_job()`, which flushes in a `finally`.
- **Bug:** a Langfuse client per turn leaked threads. One shared client per process (`app/core/tracing.py`); its exit flush is bounded by `LANGFUSE_SHUTDOWN_TIMEOUT_SECONDS` (it once hung ~90 s).
- **Gap:** a SIGKILL loses unflushed metrics.

### 29. Built-in web UI
One self-contained HTML file (`app/adapters/inbound/api/static/index.html`, no build step, no CDN) at `GET /`, talking only to `POST /chat/stream/queued`. It uses `fetch()`, not `EventSource`, because the endpoint is POST and needs identity headers.
- **Bug:** sync checkpointer calls raised `asyncio.InvalidStateError` on a live server; they are `aupdate_state`/`aget_state` now. A `MemorySaver` suite could not have caught it.
- **Bug:** a transcript was read whole. `GET .../messages` takes `limit` (1-500) and `before`, and the page loads the last 50 (R27).
- **Replacement:** `web/` (React, TypeScript, Vite; [specs/023](../specs/023-web-client/plan.md)) has the same screens and is not served yet. It exists because the single file could not be type-checked or unit-tested; reading it for the rewrite found seven bugs, listed in the plan.

### 30. Canonical error envelope
One shape, `{code, message, details}`, from one `ErrorCode` enum (`app/core/errors.py`) for every SSE `error` event and CLI error. `internal_error_envelope(exc)` returns a fixed message plus `error_class`, never `str(exc)`.
- **Bug:** both catch-alls sent `str(exc)`, leaking hosts, SQL and DSNs. Two more events carried no `code` (R24).
- **Gap:** the ingest worker's `process_job` still mixes uploader-facing messages with unexpected failures.

### 31. DB connection pool
One lazy `psycopg_pool.ConnectionPool` per database behind `get_connection()` (`app/platform/sql_store.py`); `close_pool()` runs at shutdown because pool threads outlived the process. Pools replace dead connections after the first failure ([ENGINEERING_PATTERNS.md](ENGINEERING_PATTERNS.md) 102).

### 37. Per-tool-call audit record
`MetricsCallbackHandler` joins a `tool_called` start log to its outcome by `run_id`. Args and results are logged as a truncated SHA-256 fingerprint, never raw content.

### 43. Redis Streams queue between the API and agent workers
The only HTTP chat path. `POST /chat/stream/queued` publishes a turn and relays a per-request results stream as SSE; it never runs the graph. Workers read through a consumer group, so **N workers is the scaling story** and the SSE tier and worker tier scale independently. Delivery is at-least-once: ack after `process_request` finishes, and a crashed worker's job is reclaimed (GRAPH_PATTERNS.md, "Extending Further").
- **The per-thread lock belongs to the worker**, so every front door gets it.
- **Bug:** redis-py's async `socket_timeout` (5 s) raced `XREAD`'s `BLOCK`. Use `socket_timeout=None`.
- **Bug:** the requests stream only grew (`XACK` leaves the entry). Every ack is `streams.py::ack_and_delete`, an age trim by `MINID` is the net, and **never `XADD ... MAXLEN`** (it deletes jobs nobody ran) (R04).
- **Bug:** a Redis blip ended the worker. A failed READ is retried with backoff and counted; a write is never retried and redis-py's client-wide `retry=` stays off, since it would re-send an applied `XADD` and run a second turn (R07).
- **Bug:** nothing bounded a request. `message` ≤ `CHAT_MESSAGE_MAX_CHARS`, `thread_id` 1-200 characters, images capped; `deploy/shared/caddy/gateway.caddyfile` is the outer bound (R14).
- **Bug:** a hung worker was never restarted. Workers touch heartbeat files that the container health check reads (`app/platform/worker_heartbeat.py`) and `AgentWorkerLoopStalled` fires; Compose still does not restart an unhealthy container (R22).
- **Visibility:** `agent_queue_lag`, `agent_queue_pending`, `agent_queue_wait_seconds`, `agent_first_token_seconds` and the alerts `AgentQueueBacklog`, `AgentQueueWaitHigh`, `WorkerUnreachable` size the pool (R17).
- **Resumable reads:** SSE frames carry an `id:`, a quiet stream sends `: keepalive`, and `GET /chat/stream/{request_id}` with `Last-Event-ID` reads on, owner-checked (R13). Web page and CLI do not reconnect on their own yet.
- **Cost:** three Redis round trips per token became one pipelined `MULTI`; batching tokens is not built (R08).
- **Not done:** a consumer-lag gauge, a consumer readiness probe, pruning crashed workers' consumer names, one trace per turn across the queue (R17 part 2).

### 49. Per-domain requests streams
`requests_stream_key(domain)` gives each domain its own stream and consumer group; `X-Domain` (default `ecorp`) picks one. The domain is inferred from the stream a job landed on, never the payload, so a resume or cancel lands where the turn did. An unknown `X-Domain` is a 422, not a publish nobody reads.

### 96. One worker serves many domains; a job's domain is the stream it came from
`AGENT_WORKER_DOMAINS` (names, or `all`) lets one process read every named stream in one `XREADGROUP` and run each job on that domain's graph.
- **The domain is bound for the task** (`bind_domain`, a `ContextVar`), not passed down. One checkpointer, a graph per domain; ack, dead letter and retry name the job's own stream; reclaim sweeps every served stream.
- **A typo or bad manifest stops the process at boot.**
- **Gap:** the concurrency budget is shared, so a flood on one domain can hold every slot (split the pools then); serving several domains against real Redis and a real model is untried.

### 99. Three timeouts that must stay in order are a validator, not a comment
The queue is correct only while the turn's limit (`REQUEST_TIMEOUT_SECONDS`), the conversation lock's lifetime and the reclaim idle (`AGENT_WORKER_RECLAIM_IDLE_SECONDS`) stay in order.
- **Bug:** raising the turn limit made a healthy turn run twice. `Settings` now refuses a reclaim idle that does not exceed `REQUEST_TIMEOUT_SECONDS x (1 + UNATTENDED_MAX_DECLINE_ROUNDS)` and the lock lifetime, each by 30 s, naming the variables, never the values.
- **Bug:** the lock was never renewed, so a second job could take it mid-turn. The worker renews it every third of its 60 s lifetime with a compare-and-extend script; a lost lock is counted (`AgentThreadLockLost`).
- **Bug:** a worker claimed jobs it could not start, so "idle" measured the queue. It now takes free slots first (`claim_free_slots`).
- **Rule:** write a timeout relation as a check that fails at start-up, and make its test name the variables.
