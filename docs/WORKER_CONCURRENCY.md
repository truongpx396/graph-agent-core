# Worker concurrency: how we decided, what we built

The reasoning behind scaling `app/adapters/inbound/workers/agent_worker.py` and `app/adapters/inbound/workers/ingest_worker.py` for concurrent requests, why both ended up on one concurrency model despite very different work, and how to size them. It's a scaling/ops decision record, so it lives apart from the patterns ([RUNTIME_PATTERNS.md](RUNTIME_PATTERNS.md) has the queue's).

## The starting question

`POST /ingest/upload` (`app/adapters/inbound/api/routers/ingest.py`) uploads to MinIO, publishes a job onto `ingest:requests` (`app/adapters/outbound/redis/ingest_queue.py`) and returns immediately; `app/adapters/inbound/workers/ingest_worker.py`, a separate consumer, does the parsing and embedding. `POST /chat/stream/queued` uses the same producer/consumer split against `app/adapters/inbound/workers/agent_worker.py` via `app/adapters/outbound/redis/queue.py`. The question: **if 50 requests land at once, do we need 50 worker containers?** No.

## Why 1:1 (requests : containers) is the wrong model

Both endpoints decouple "accepted" from "processed": 50 simultaneous requests are just 50 jobs on a Redis stream. The queue absorbs the burst, workers drain it at their sustainable rate, and nobody's HTTP request waits on a worker. So worker count is sized to the **real bottleneck**, which differs by worker:

- **`ingest_worker`:** CPU (PDF/DOCX extraction in `app/ingestion/extractors.py` is synchronous and CPU-bound), the embedding endpoint's rate limit, and Qdrant/MinIO throughput.
- **`agent_worker`:** the LLM backend's own real concurrency (see `AGENT_WORKER_MAX_CONCURRENCY` in `app/core/config.py`, and `loadtest/fake_llm_server.py`'s note on native Ollama's hard `-np 1` ceiling versus a genuinely concurrent backend).

Provisioning to the burst peak when the ceiling is elsewhere just pays for idle containers waiting on the same choke point.

## How to size worker capacity

1. **A fixed baseline for steady-state load,** not burst peaks: a handful of replicas.
2. **Autoscale on queue depth or consumer lag,** not request rate (`XLEN`/`XPENDING` on either stream): a growing backlog scales the service up (`docker compose --scale`, or an HPA equivalent), an empty queue scales it down. This reacts to *sustained* load, not a burst that clears in seconds. Nothing does this yet; it's the missing piece flagged in RUNTIME_PATTERNS.md 43 (an orchestrator that scales on queue depth).
3. **A ceiling on scale-out** set by the real bottleneck (the embedding API's rate limit; the LLM backend's concurrency).
4. Users see "N requests accepted instantly, processed over the next seconds or minutes" (why both endpoints stream per-job progress over SSE).

## One process per container

Docker already gives independent restart-on-crash, resource accounting and trivial scaling *per container* (`restart: unless-stopped` is container-level, so a crashed process inside a multi-process container needs its own supervisor to be noticed at all). Both workers are one-process-per-container services in `deploy/dev/app.yml` and `deploy/prod/app.yml`, scaled by replica count (`docker compose --profile app up -d --scale agent-worker=3` / `--scale ingest-worker=3`). Efficiency belongs *inside* the process (below), not in packing processes together. Each replica pushes its metrics over OTLP to one collector (RUNTIME_PATTERNS.md 43), so scaling needs no scrape-config change either.

## Domains per process

One `agent_worker` process serves every domain in `AGENT_WORKER_DOMAINS` (names, or `all`), so the four example domains need one container, not four. The budget is the process's: `AGENT_WORKER_MAX_CONCURRENCY` turns at once **across all its domains**, over one checkpointer pool. Size it for the sum of the domains' load, not per domain.

The cost is isolation. A flood on one domain can occupy every slot while another waits. If that matters, run separate pools, each with its own `AGENT_WORKER_DOMAINS` and replicas (`AGENT_WORKER_DOMAINS=support docker compose --profile app up -d --scale agent-worker=2`); streams and consumer groups are per domain, so the pools never share a job. RUNTIME_PATTERNS.md 96 has the mechanics.

## Model 1: `agent_worker`, concurrency because turns mostly *wait*

`agent_worker.py::run()` runs up to `AGENT_WORKER_MAX_CONCURRENCY` turns in ONE process (all its domains together): an `asyncio.Semaphore` acquired *before* a task is created, then `asyncio.create_task` per job, not a serial `await` loop.

- **Why "50 parallel requests" doesn't need 50 cores:** a turn's slow part is a network round trip to an LLM API plus tool execution, during which the process is idle. asyncio overlaps many such waits on one thread: while turn A awaits the LLM, the loop runs turn B. That's **concurrency** (many outstanding I/O operations interleaved), not **parallelism**. If the "LLM call" were local CPU-bound computation, it wouldn't work (see Model 2).
- **Why it's safe:** a turn holds no in-process state a concurrent sibling could corrupt. Conversation state lives in Postgres via the checkpointer; tenant-scoped resources (Redis, Qdrant, the appdata pool) are per-call or pooled; and `bind_request_id`'s `contextvars.ContextVar` is copied per task by `asyncio.create_task`, so one task's request id never leaks into a sibling's logs.
- **The bottleneck it doesn't remove:** the checkpointer. `PgCheckpointing.open` (`app/adapters/outbound/postgres/checkpointing.py`) backs `AsyncPostgresSaver` with a connection *pool* because psycopg wraps every operation on one connection in its own lock, so a single shared connection would serialize every turn's checkpoint reads and writes regardless of the semaphore. The pool is what lets `_MAX_CONCURRENCY` turns overlap.

## Model 2: `ingest_worker`, originally serial, now mirrors Model 1

It originally processed one job at a time (`_READ_COUNT = 1`, a sequential `await` loop). Not because jobs have unsafe shared state, but because download (`object_store.download_bytes`) and extraction (`extract_pdf_text`/`extract_docx_text`) were **synchronous blocking calls on the event loop thread**. Running several jobs via `asyncio.create_task` without wrapping them wouldn't have overlapped anything: the first job's extraction would hog the one thread, starving every sibling's I/O (including its own progress publishes). This is the mirror image of Model 1: turns are I/O-bound *and already async*; ingest jobs are partly CPU-bound *and were called synchronously*.

`ingest_worker.py` now matches `agent_worker.py`:

- **`INGEST_WORKER_MAX_CONCURRENCY`** (`app/core/config.py`, default `10`): the semaphore bound.
- **`run()`** acquires a slot *before* creating each job's task (so a full semaphore also backpressures reading from the stream), dispatches `asyncio.create_task(_process_with_limit(...))`, and tracks in-flight tasks so a graceful shutdown lets claimed jobs finish and ack instead of abandoning them.
- **`process_job`** wraps `object_store.download_bytes` and the extractor in `asyncio.to_thread` (on top of `ingestor.ingest_text`, which already ran that way). This is *what makes* `_MAX_CONCURRENCY > 1` real here; without it, concurrency would be cosmetic.

### A gap only Model 2 has: the thread pool `to_thread` borrows from

`agent_worker.py` touches no thread pool (every turn is awaited async I/O), so `AGENT_WORKER_MAX_CONCURRENCY` is the only limit. `ingest_worker.py`'s `to_thread` calls borrow from the event loop's **default executor**, sized by Python as `min(32, os.cpu_count() + 4)` and unrelated to `INGEST_WORKER_MAX_CONCURRENCY`. That's a silent ceiling: raising the setting past 32 would admit more jobs than the pool can serve, the excess quietly queueing for a thread; and below 32, whether the default lands above or below the setting is an accident of the host's core count.

`run()` now sets the executor explicitly at startup: `loop.set_default_executor(ThreadPoolExecutor(max_workers=_MAX_CONCURRENCY, ...))`. Exactly `_MAX_CONCURRENCY`, not padded by core count: a job's `to_thread` calls are sequential (download, extract, embed), so one in-flight job never holds more than one pool thread. Core count is left out on purpose: the GIL, not thread count, bounds CPU-bound throughput, and `_MAX_CONCURRENCY` is already the operator's CPU-vs-I/O tuning knob; folding core count back in would silently override it.

### The honest limitation Model 2 still has

`asyncio.to_thread` moves a blocking call off the event loop onto a real OS thread, but the GIL still serializes CPU-bound bytecode across threads. When extraction is the bottleneck (a huge PDF), several concurrent extractions get no parallel CPU throughput; they only overlap the *I/O* of sibling jobs (MinIO downloads, embedding HTTP calls, Qdrant upserts, progress publishes). Model 1 has no such step: an LLM call isn't local CPU work. For CPU-dominated ingest, more **replicas** add real parallelism; `_MAX_CONCURRENCY` is the lever when jobs spend more time waiting on MinIO, the embedding endpoint or Qdrant than parsing. They compose: N replicas × `_MAX_CONCURRENCY`.

## Sizing the two knobs

Neither comes from a formula:

- **`AGENT_WORKER_MAX_CONCURRENCY`** (default `10`) depends on the LLM backend's real concurrency; a load-test run dials it without a redeploy (see the setting's comment in `app/core/config.py`).
- **`INGEST_WORKER_MAX_CONCURRENCY`** (default `10`, matching the agent worker as a starting point, not a measured optimum) depends on how I/O-dominated the real document mix is. Raise it if profiling shows a worker idle-waiting on MinIO, the embedding endpoint or Qdrant more than extracting; lower it, and add replicas, if extraction dominates.

## Reading from the queue, and the three timings

- **A worker reads only what it can start.** It takes its free slots first and asks `XREADGROUP` for that many, so a worker at its limit reads nothing and the entries stay pending for the group. Before this a full worker claimed up to its whole concurrency and waited on the semaphore for each entry; a claimed entry's idle clock runs while it waits, so under load "idle" measured the queue, not a dead worker, and a healthy job was reclaimed and run twice (pattern 99).
- **Three numbers must stay in order**, and `Settings` refuses a configuration where they do not: `REQUEST_TIMEOUT_SECONDS` (a turn's wall-clock limit, 60 s), the conversation lock's lifetime (a 60 s constant, renewed every 20 s by the worker that holds it), and `AGENT_WORKER_RECLAIM_IDLE_SECONDS` (300 s). The idle must exceed `REQUEST_TIMEOUT_SECONDS x (1 + UNATTENDED_MAX_DECLINE_ROUNDS)` and the lock's lifetime by at least 30 s: an unattended turn runs that many streams in a row. **Raising `REQUEST_TIMEOUT_SECONDS` means raising the idle with it** (a 300 s turn needs at least 1230 s). The ingest worker has no turn limit; `INGEST_WORKER_RECLAIM_IDLE_SECONDS` (900 s) stands alone.
- **`REQUESTS_STREAM_MAX_AGE_SECONDS`** (86400) is the net under "acknowledge, then delete": a reclaim pass drops requests-stream entries older than that. It must exceed both reclaim idles. The worker deletes each job as it acknowledges it, so in a healthy deployment the stream holds only what is waiting or running; a requests stream whose length keeps growing is the symptom of a worker that does not.
- **A Redis blip is survived, not fatal.** A failed read is retried after 0.5 s, doubling to 5 s, and counted (`agent_worker_queue_read_failed_total`); a worker told to stop during the wait stops at once.

## Seeing the queue: the three numbers that size the pool

Nothing here scales on queue depth, and until now nothing could even see it. Three measurements say whether the pool is the right size, and each has a panel on the agent dashboard ("Queue & first token") and, for the first two, an alert:

- **`agent_queue_lag{domain}` and `agent_queue_pending{domain}`**: jobs on the requests stream that no worker has been given yet, and jobs a worker has been given and not acknowledged. Every worker's reclaim pass reads them from `XINFO GROUPS` and sets them (`ingest` is the upload queue). A lag that keeps rising means the pool takes jobs slower than they arrive: `AgentQueueBacklog` fires above 20 for five minutes.
- **`agent_queue_wait_seconds{queue}`**: how long a job sat on the stream before a worker started it, from the `enqueued_at` the API stamps on every job. `AgentQueueWaitHigh` fires when its p95 passes ten seconds, which says the pool is undersized before a lasting backlog does. A job from an older API during a rolling deploy carries no stamp and is not observed; a reclaimed job keeps its first stamp, so a crashed worker is one long wait.
- **`agent_first_token_seconds`**: from the start of a turn's run to the first token it sends, which is what a person waits before they see anything. It does not include the queue wait; add the two for the whole wait.

These are what the sizing above should be judged against: raise `AGENT_WORKER_MAX_CONCURRENCY` or add replicas when the wait and the lag rise while the workers' own CPU and the model backend have room, and not when the first-token time is the problem (that is the model, not the pool).

## What one streamed event costs the worker

Every event of a turn (a token, a tool start) was three Redis round trips awaited one after another on the model's own stream: the cancel check (`GET`), then the `XADD` and the `EXPIRE` that put the event on the results stream. An answer of 500 tokens was 1,500 round trips, and each held up the next chunk. Now the `XADD` and the `EXPIRE` are one pipelined `MULTI` (`streams.py::publish_result`; the same for the ingest worker), and the cancel check runs at most every 250 ms and always before the first event, so a stop takes effect within a quarter of a second at an event boundary, as before.

Measured on a real Redis over loopback, 500 events: 232 ms before, 123 ms after (0.46 to 0.25 ms per event, median of five runs). Loopback understates it: the saving grows with the network round-trip time between the worker and Redis. **Step 2 is not done:** batching what is waiting into one write (joining adjacent tokens) needs `make loadtest-queued-headless` before and after, and the work order says not to merge it if the gain is small. That was not measured.

## Postgres connection budget

Every process that runs the graph (each `agent-worker` replica **and** the API, via `init_graph_async()` in its lifespan) holds two client-side pools: the appdata pool (`app/platform/sql_store.py`, `APPDATA_POOL_MAX_SIZE`, default `10`) and the checkpointer pool (`CHECKPOINTER_POOL_MAX_SIZE`, default `10`). Direct to Postgres, scaling replicas multiplied that, and nothing enforced the ceiling:

    20 × (agent-worker replicas + API replicas)  ≤  max_connections − reserved

Postgres's default `max_connections` is `100` (3 reserved for superusers), which fits only about four such processes before counting anything else on the server (LiteLLM and Langfuse share it).

**PgBouncer takes replicas out of that sum** (specs/022). The containers reach `appdata` and `checkpointer` through the `pgbouncer` service in transaction mode, where a server connection is held for one transaction, so the client-side pools above are cheap (a few KB each in the pooler) and Postgres sees only the pooler's own pool:

    2 databases × MAX_DB_CONNECTIONS (15 pool + 3 reserve = 18)  +  direct clients (LiteLLM, Langfuse, exporters, an operator's psql)  ≤  max_connections

That is 36 + 30 headroom ≤ 100, written into both compose files and checked by `tests/core/test_compose_pooler.py`, so raising `DEFAULT_POOL_SIZE` past the budget fails the build instead of the first busy afternoon. Scaling `--scale agent-worker=N` no longer moves it. What replaced the old failure is **waiting**: past 15 simultaneous transactions per database a client queues for a server connection (alert `PgBouncerClientsWaiting` at 5 minutes, `PgBouncerMaxWait` at 10 s; `QUERY_WAIT_TIMEOUT` gives up at 30 s). Transactions here last milliseconds, so queueing means a transaction is being held open (look for `idle in transaction` first; `IDLE_TRANSACTION_TIMEOUT` cuts one at 60 s), not that the pool is too small.

What the pooler costs the code: **no prepared statements and no session state.** Every pool passes `prepare_threshold=None` (`POOLER_SAFE_CONNECTION_KWARGS`; psycopg's `0` means "prepare on the first run", the opposite), and a scan (`tests/core/test_pooler_safe_sql.py`) fails the build on a `SET` that is not `SET LOCAL`, a session advisory lock, `LISTEN`, a temp table and the like, because a pooled server connection is lent to a different tenant's transaction next. `tests/integration/test_pgbouncer.py` runs the app's own pools against a real PgBouncer that refuses prepared statements, and shows the previous settings failing. `make billing-migrate` takes only transaction-scoped locks (`pg_advisory_xact_lock`, `SET LOCAL`) and was run twice through the pooler by hand (it applied, then found everything current; not in CI), so the pooled address works for it too; an interactive `psql` that wants a session `SET`, and the init scripts, use Postgres on `:5432`.

**What exhaustion looks like:** pool checkouts fail with `FATAL: sorry, too many clients already`, turns fail *inside* an already-open SSE stream, and the client sees HTTP 200 with no answer text. That exact failure took down `tests/integration/test_worker_scaling.py`'s 250-turn test (245 of 250 empty) until `tests/containers.py::ensure_postgres` got `max_connections=300` (5 workers + the API alone is 6 × 20 = 120 potential connections).

## A different knob: the UI's per-request file cap

`MAX_UPLOAD_FILES_PER_REQUEST` (`app/core/config.py`, default `5`, enforced in `POST /ingest/upload` and mirrored in `app/adapters/inbound/api/static/index.html`'s upload form) caps how many files ONE submission may batch. It's a UX/abuse guard on one HTTP request, not a worker setting: a user can submit several batches back to back, and all `ingest-worker` replicas × `INGEST_WORKER_MAX_CONCURRENCY` drain them concurrently however they were batched. Raising the cap adds no ingest throughput, and raising `INGEST_WORKER_MAX_CONCURRENCY` doesn't change how many files one form may include.
