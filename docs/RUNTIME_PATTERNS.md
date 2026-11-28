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
- **Bug:** a Langfuse client per turn leaked threads. One shared client per process (`app/core/llm_tracing.py`); its exit flush is bounded by `LANGFUSE_SHUTDOWN_TIMEOUT_SECONDS` (it once hung ~90 s).
- **Gap:** a SIGKILL loses unflushed metrics.

### 29. Built-in web UI
The client in `web/` (React, TypeScript, Vite; [specs/023](../specs/023-web-client/plan.md)), built to static files: `index.html` at `GET /` and the bundle under `/static/assets/` (both public at the front door), with no third-party script. In containers its own image serves them (pattern 116); under `make serve` the API does. It talks only to the published endpoints and renders only the documented stream events, with types generated from `openapi.json`. It uses `fetch()`, not `EventSource`, because the endpoint is POST and needs identity headers.
- **Bug:** sync checkpointer calls raised `asyncio.InvalidStateError` on a live server; they are `aupdate_state`/`aget_state` now. A `MemorySaver` suite could not have caught it.
- **Bug:** a transcript was read whole. `GET .../messages` takes `limit` (1-500) and `before`, and the page loads the last 50 (R27).
- **Bug:** one hand-written 2,000-line HTML file could not be type-checked or unit-tested, and seven bugs sat in state shared between its handlers (an approval pause left the rest of the answer styled as a draft; events from a left conversation ended the next one's turn; the full list is in [specs/023/plan.md](../specs/023-web-client/plan.md)). The turn is now a pure event reducer inside feature modules whose imports a test checks.
- **Gap:** a dropped stream is not read on with `Last-Event-ID`.
- **Gap:** bundle file names carry a content hash, so while the server of the page is being replaced a page from a new one can ask an old one for a file it does not have (not observed; no deploy has run).
- **Bug:** the transcript jumped to the end on every token, so a reader could not scroll up while an answer streamed. It follows only while the reader is at the end, and reads the position before each update too: a token can arrive between a scroll and its event.

### 30. Canonical error envelope
One shape, `{code, message, details}`, from one `ErrorCode` enum (`app/core/errors.py`) for every SSE `error` event and CLI error. `internal_error_envelope(exc)` returns a fixed message plus `error_class`, never `str(exc)`.
- **Bug:** both catch-alls sent `str(exc)`, leaking hosts, SQL and DSNs. Two more events carried no `code` (R24).
- **`details.kind`** says what to make of it: `blocked` (a rule said no), `failed`, or `lost` (what was waited for or answered is gone). `to_dict` adds it; a new code must be classified. A cancel has none, nor have the four codes the front door also answers (the two must answer alike).
- **Gap:** the ingest worker's `process_job` still mixes uploader-facing messages with unexpected failures.

### 31. DB connection pool
One lazy `psycopg_pool.ConnectionPool` per database behind `get_connection()` (`app/platform/sql_store.py`); `close_pool()` runs at shutdown because pool threads outlived the process. Pools replace dead connections after the first failure ([ENGINEERING_PATTERNS.md](ENGINEERING_PATTERNS.md) 102).

### 37. Per-tool-call audit record
`MetricsCallbackHandler` joins a `tool_called` start log to its outcome by `run_id`. Args and results are logged as a truncated SHA-256 fingerprint, never raw content.

### 43. Redis Streams queue between the API and agent workers
The only HTTP chat path. `POST /chat/stream/queued` publishes a turn and relays a per-request results stream as SSE; it never runs the graph. Workers read through a consumer group, so **N workers is the scaling story** and the SSE tier and worker tier scale independently. Delivery is at-least-once: ack after `process_request` finishes, and a crashed worker's job is reclaimed (GRAPH_PATTERNS.md, "Extending Further").
- **The per-thread lock belongs to the worker**, so every front door gets it.
- **Bug:** redis-py's async `socket_timeout` (5 s) raced `XREAD`'s `BLOCK`. Use `socket_timeout=None`.
- **Bug:** an approval prompt never appeared; the person saw "is an agent-worker running?". The API reads 30 s of silence as "no worker", but a turn publishes nothing until its first token and a tool call has none: the model took 28.6 s, and the pause was published 0.3 s after the deadline. A held conversation lock now restarts the wait (trace `7e6cad06`). A worker now also says `started` before it runs anything (117).
- **Bug:** the requests stream only grew (`XACK` leaves the entry). Every ack is `streams.py::ack_and_delete`, an age trim by `MINID` is the net, and **never `XADD ... MAXLEN`** (it deletes jobs nobody ran) (R04).
- **Bug:** a Redis blip ended the worker. A failed READ is retried with backoff and counted; a write is never retried and redis-py's client-wide `retry=` stays off, since it would re-send an applied `XADD` and run a second turn (R07).
- **Bug:** nothing bounded a request. `message` ≤ `CHAT_MESSAGE_MAX_CHARS`, `thread_id` 1-200 characters, images capped; `deploy/shared/caddy/gateway.caddyfile` is the outer bound (R14).
- **Bug:** a hung worker was never restarted. Workers touch heartbeat files that the container health check reads (`app/platform/worker_heartbeat.py`) and `AgentWorkerLoopStalled` fires; Compose still does not restart an unhealthy container (R22).
- **Visibility:** `agent_queue_lag`, `agent_queue_pending`, `agent_queue_wait_seconds`, `agent_first_token_seconds` and the alerts `AgentQueueBacklog`, `AgentQueueWaitHigh`, `WorkerUnreachable` size the pool (R17).
- **Resumable reads:** SSE frames carry an `id:`, a quiet stream sends `: keepalive`, and `GET /chat/stream/{request_id}` with `Last-Event-ID` reads on, owner-checked (R13). The web page reads on by itself (121); the CLI does not yet.
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

### 112. One request, findable in every service's logs, and no secret in any of them
Every line is JSON. The ids of the request or job in progress are bound once (`app/core/log_context.py`: `request_id`, `thread_id`, `tenant`, `principal`, `domain`, `job_id`) and the formatter stamps them on every line below, with the `service` name.
- **`request_id` is what the client was given** in `X-Request-Id`. A queued chat route answers with the job's id, so the API, the worker and the front door's access line share it; every other response gets one minted. An approval resume is a new request: `thread_id` joins the two. An upload logs `ingest_job_queued`, which joins its request to the job ids.
- **One access line per request** (`api/request_log.py`): method, route, path, status, duration; written when an SSE stream ends. Probes and static files only when they fail. uvicorn's own access log is off.
- **Collected and followed:** Promtail ships the app and the front door and normalises level names; the `Agent Core Logs` dashboard has boxes for a request id, a thread id and text (in Explore's Table view, `request_id` is also a link). Ids are never Loki labels (each value would be a stream).
- **Langfuse** traces carry `request_id`, `domain` and `tenant` as metadata; their session is the thread.
- **Bug:** `httpx` logged every URL at INFO and the Telegram Bot API carries the token in the path. `httpx` is held at WARNING and every string value is scrubbed (a traceback quotes the URL too).
- **Bug:** the front door's access and error lines held a sign-in's `code` and Telegram's webhook secret header. They now hold no query and no header (`deploy/shared/caddy/redacted-format.caddyfile`).
- **Bug:** the dashboard defaulted to `error|warn`: Python writes `warning`, so every warning was hidden, and the ids were on `info` lines.
- **Principals are logged** as the identity provider names them, which may be an email: treat the log store as holding personal data.
- **Gap:** the MCP gateway, the Telegram bot and `authlimit` bind no per-request id; the ML service logs plain text. Third-party containers keep their own formats.

### 113. One trace id from the request to the model call, and Langfuse uses it too
`app/core/spans.py` records where a turn's time went: the API request, the worker's job (the W3C context travels in the queued job), each node, each model and tool call. OTLP to the collector, then Tempo; Grafana links a line's `trace_id` to its trace and a span back to its lines.
- **One id.** `trace_id` on a log line, `X-Trace-Id` on a response, the Tempo trace and the turn's **Langfuse trace id** are the same value. Tempo is the timings, Langfuse is what the model saw.
- **`request_id` is not a trace id:** it names a job (`X-Request-Id`); `thread_id` names a conversation; a resume is a new request and a new trace.
- **Never mixed:** a later run in the same request sends Langfuse no name or input (upsert on id); a job run again after a crash gets its own trace, linked; a client's `traceparent` is ignored; the LiteLLM proxy is never sent `trace_id` or `existing_trace_id`, which would merge its per-call records into ours and count each call twice. Its own records keep its own ids.
- **Metadata only.** Names, counts, ids and an error's class: `record_exception` is off, and our `TracerProvider` is not the global one, so a library's spans are not exported.
- **Gap:** the trace starts at the API (not Caddy or authlimit) and has no spans for outbound HTTP, Postgres or Redis; their time is inside the node that called them. [specs/024](../specs/024-distributed-tracing/spec.md) has the decisions.

### 116. The web client is its own container
`web/Dockerfile` (Vite builds with Node, Caddy serves) and the `web` service, in the dev stack and production. The front door sends it `/` and `/static/*`; everything else goes to the API as before. In the dev stack its port is a whole app on one origin (http://localhost:8085): it passes on the paths the client requests. What the page is told about sign-in is written per request from the container's environment (`web/server/agent-auth.tmpl`), so one image serves every environment. `web_auth.page_config` writes the same for `make serve`, and `web/server/testdata/agent-auth.json` holds the two to one answer.
- **Why:** the page was a stage of the API's image: nothing showed a web app running, a style change rebuilt the API, and the Python process served static files.
- **Bug (building it):** the official Caddy binary carries a file capability, and under `cap_drop: ALL` the kernel refuses to execute it (`operation not permitted`, as root too). The image removes the capability. `scripts/checks/web_image.py` runs the built image under the same hardening, which a build alone does not show.
- **Gap:** no access log (a sign-in returns to `/?code=...`; the front door's redacted log covers the page there), no `script-src` policy, and one replica in production.

### 117. A turn says whether a worker has it
A turn's or resume's results stream starts with `{"type": "status", "state": "queued"}`, written by the producer in the same `MULTI` as the job, and the worker that takes it publishes `started` (an opaque `w-<hex>`, never the host name) before anything runs. A cancel has neither: it is answered by its outcome. [specs/025](../specs/025-agent-runtime-contract/spec.md) is the contract.
- **Why:** the first event a client saw was the first thing the model did, so "no worker has this yet" and "the model is thinking" looked the same.
- **The first-response deadline waits for a worker's event.** `queued` does not end it. A read from a position asks the stream whether a worker ever wrote (`XRANGE ... COUNT 2`): until `queued`, no client could hold a position before one had, and a reconnect with no worker would now wait for ever.
- **Order in the worker:** deal with the stop flag, then say `started`. The other order erases a stop a client sends on seeing it.
- **Bug:** a stop pressed while the turn was still queued did nothing: the worker removed every stop flag as it took a job, and the turn ran once a worker was free. The flag now holds WHEN stop was pressed and lives as long as a job can wait. A worker keeps and honours a flag newer than the job's `enqueued_at`, and removes an older one, in one Lua script: a queued turn runs nothing, a queued resume runs nothing and ends the pause. Both times are the API's clock.
- **A new event type is a major API version.** oasdiff reports a member added to a response union, or a value added to a response enum, as breaking. So 4.0.0 declares `status`, `usage`, `system_note` and the code `approval_superseded` together, and a vocabulary that may grow is a string with `x-extensible-enum`.
- **Gap:** an unattended turn crashed and continued has no cancel check, so a stop does not reach it. Replicas of the API with clocks apart by more than the time between a send and a stop would judge the order wrongly. No queue position. Only the web page says "waiting to start" (121).

### 121. The web page shows what the server says, and nothing it guessed
Each tool call is a row with the server's label, tier, outcome, duration and summary. A pause is a card above the composer built from its `items`. A turn ends with its model, tokens and cost, the header sums them, and `details.kind` decides how a stop reads: only `failed` is red. The rules for changing the UI are in [web/DESIGN.md](../web/DESIGN.md); `web/src/test/design.test.ts` holds the ones a machine can.
- **No identifier and no JSON on screen,** except the card's "Technical details", closed by default. A call the server does not name is "A step".
- **Bug:** the page sent no `approval_id`, so an old tab could approve a newer pause. It sends the id it showed; if that pause was replaced, nothing runs and the page shows the one waiting now.
- **A person's answer is claimed only once the graph goes on.** Until then a decline is not written up, and an answer that is refused (a rate limit) or fails brings the card back.
- **The card never takes the focus,** and Enter approves only while it has it. A global shortcut would approve a write for someone typing.
- **"Waiting to start" after 10 s without `started`.** The server gives up on a worker after 30 s (`CHAT_FIRST_RESPONSE_DEADLINE_SECONDS`), so a later threshold would never be seen.
- **A dropped stream is read again** from `Last-Event-ID`, five attempts over about 30 s. A refusal is an answer and is not retried. A response without `X-Request-Id` cannot be read again.
- **Gaps:** no queue position (the server sends none). History is text only, so a reopened conversation has no tool rows or usage, and the header's total covers only turns answered in this tab since the conversation was opened. The sidebar's "Try asking" tags name tools on purpose. The CLI and Telegram show neither the rows nor the usage.
