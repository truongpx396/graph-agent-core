---
paths:
  - "app/agent/graph/**/*.py"
  - "app/agent/runtime/**/*.py"
  - "app/adapters/outbound/redis/**/*.py"
  - "app/adapters/inbound/workers/**/*.py"
  - "app/adapters/inbound/api/main.py"
  - "app/adapters/inbound/api/deps.py"
  - "app/adapters/inbound/api/routers/**/*.py"
  - "app/core/metrics.py"
  - "app/core/resilience.py"
  - "deploy/dev/litellm-patches/**"
  - "observability/**"
---

# Graph, runtime, queue and observability gotchas

Constitution Principles V and VI apply. Each item below exists because it cost a real debugging
session; the pattern documents (`docs/README.md` maps each number to its file) have the full story under the pattern number given.

## Graph (`app/agent/graph/`)

- Every prompt is a domain's own text plus the platform's rules (`app/core/prompt_rules.py`, added by `build_graph`, pattern 114): a rule for
  every agent goes there, never into one domain. Outside text a tool returns goes through `frame_untrusted`, and a tool's docstring is read by
  the model, so developer notes are comments. A domain's text is its `prompt.md`; run `make prompt-check` before and after editing one.
- A system prompt stays a ctx-free constant (pattern 19). Never interpolate a tenant, principal,
  or timestamp; `tests/agent/graph/test_prompt_cache_stability.py` guards it.
- Names follow one rule (docs/GRAPH_PATTERNS.md "Naming", `tests/agent/graph/test_graph_naming.py`): the node registered as `"<node>"` is
  `graph_node_<node>` (a factory is `make_graph_node_<node>`), and the router of its conditional edge is
  `graph_edge_route_after_<node>`. The registered string is the node's identity (traces, metrics, checkpoints: a checkpoint names what is
  pending after the target node, pattern 16), so a function can be renamed but a node's string cannot without draining the workers. A
  conditional edge is registered under its entry in `EDGE_CHANNEL_NAMES`, never under its router's name; a new one adds its entry.
- Nodes are wrapped by `instrumented` in `build_graph` at registration time, not in the node
  body. `human_approval`'s `interrupt()` is a pause (`node_paused`), not a failure — re-raise it.
  Never log message content or the `state` dict.
- Anything that rejects or skips pending tool calls (HITL rejection, over-budget batch) MUST emit
  one `ToolMessage` per pending `tool_call`, or the next LLM call fails (patterns 8, 10).
- History trimming is turn-aware: never split a `tool_call` from its `ToolMessage` (pattern 13).
- `retry_output`'s feedback is a `HumanMessage` tagged `RETRY_ARTIFACT_KEY` (as is the answer it rejected), not something the person said. Find the user's message or the turn start with `is_user_message`, never a bare `isinstance(m, HumanMessage)`; never tag an `AIMessage` that has `tool_calls` (pattern 5).
- `validate_input` resets `iterations`/`total_tokens` every turn but NOT on a HITL resume.
- A breaking `State` / topology change bumps `STATE_SCHEMA_VERSION`; a differing build SHA alone
  is not a break (`resumability_error_async`, pattern 16).
- `build_graph` is the one builder; `build_subagent_graph` is it with `SUBAGENT_PROFILE`, which
  deliberately omits the cache/follow-up/compaction nodes. A new node is registered in `build_graph`;
  decide which `GraphProfile` flags it belongs behind. Nothing a node imports may import `graph/build.py`.
- A new output check is one `OutputCheck` entry in `graph/output_checks.py` (its position is its priority),
  not a `State` flag.
- Every graph caller is async. `AsyncPostgresSaver`'s lock is bound to the loop that opened it:
  open it through `init_graph_async` on the calling loop. `MemorySaver` is for tests/subagents.
- Don't reorder the bound tool list: `skill_search`/`use_skill` sit first on purpose (pattern 50).
- Parallel tool calls are supported. The old glued-together `tool_calls` corruption was a LiteLLM
  chunk-index bug, fixed in `deploy/dev/litellm-patches/sitecustomize.py` (loaded via `PYTHONPATH` in
  `deploy/dev/infra.yml`) — not by `parallel_tool_calls=False`, which was a no-op and has been removed.
  Treat that patch file as load-bearing.

## Streaming turns (`runtime/stream.py`)

- A loop over `astream_events_*` or `_run_graph_stream` that can leave early (an exception, a `return`, a `break`) uses `contextlib.aclosing`. A bare `async for` does not close the generator when it is left, so the turn's own cleanup (the trace, the abandoned-turn metrics, the budget hold) runs a loop turn late, after the failure was handled. A close mid-turn is `GeneratorExit` in `_run_graph_stream` and is recorded as the `abandoned` outcome (pattern 36). The three entry points share `_run_turn` and `_BudgetHold` (the hold is released on every exit): that puts one more generator between an entry point and `_run_graph_stream`, so **each entry point iterates `_run_turn` under `aclosing` too**, which the first draft of the extraction forgot and the early-close tests caught. `_run_turn` calls its input builder INSIDE the trace, so a failure while building a turn's input still finishes the trace as an error.

## Queue and workers (`outbound/redis/`, `inbound/workers/`)

- Delivery is at-least-once: ACK only after `process_request` finishes, so every handler MUST be
  safe under redelivery.
- Reclaim never restarts blindly. A crashed `"turn"` is judged by `_classify_reclaimed_turn` and
  continued via `astream_events_continue_turn`, because a restart re-asks the LLM and mints new
  `tool_call_id`s.
- One requests stream per domain (`requests_stream_key`). Domain is inferred from the stream a
  job landed on, never from its payload; an unknown `X-Domain` is a 422.
- A blocking `XREAD` needs `socket_timeout=None` on the redis-py async client (its 5s default
  races the server-side `BLOCK`).
- An ack is `streams.py::ack_and_delete` (`XACK` and `XDEL` in one MULTI), never a bare `xack`: `XACK` leaves the entry in the stream for good. Never bound a requests stream with `XADD ... MAXLEN` (under a backlog it deletes jobs nobody has run); the age trim is by `MINID` (pattern 43).
- Retry a failed READ of a stream (`redis_client.UNAVAILABLE_ERRORS`), never a write, and never turn on redis-py's client-wide `retry=`: it would re-send an `XADD` the server already applied, and a second `turn` job runs as a second turn (pattern 43).
- Three numbers must stay in order: the turn's limit, the conversation lock's lifetime, and the reclaim idle. `Settings` refuses a reclaim idle at or below the longest turn plus 30 s; a deployment that widens `REQUEST_TIMEOUT_SECONDS` sets `AGENT_WORKER_RECLAIM_IDLE_SECONDS` with it. A worker takes its free slots before it reads and asks for that many, so a claimed entry never waits for a slot (pattern 99).
- The production queue Redis has `noeviction` and a `maxmemory`: when it is full a write is refused (a new turn fails to publish) and nothing is dropped; the answer cache is a different Redis (`SEMANTIC_CACHE_REDIS_URL`) that evicts. Never put the cache back on the queue's Redis, and never turn persistence on for a volume that has data without enabling it in the running server first (docs/ENGINEERING_PATTERNS.md 100).
- Every wait for a counterparty has a deadline (`read_results(first_event_deadline_seconds=...)`),
  and a claim that wins but whose publish fails is compensated (`release_submission_claim`). The producer's own `queued` status is not the
  counterparty answering: only a worker's event ends that deadline (RUNTIME_PATTERNS.md 117).
- The chat stream's event `type`s and the `ErrorCode` values are in the contract: a new one is a break `make openapi-breaking` refuses without a
  new major version (pattern 117). A new optional field is not.

## Resilience (`core/resilience.py`)

- `CircuitBreaker.call` retries only exceptions the caller names in `retry_on` and can justify as
  "never landed". Don't wrap a raw sandbox command or any non-idempotent write in it. Half-open
  admits exactly one trial under the lock.

## Logs

- An entry point opens `log_context.scope(...)` and code that learns an id later calls `log_context.add(...)` (RUNTIME_PATTERNS.md 112); never pass a request id down by hand. Ids and names only: no message content, no URL with a query or a token.
- A new library that logs each outbound URL goes in `logging_config._QUIET_LOGGERS`. A new service goes in BOTH Promtail allowlists.

- Spans go through `app/core/spans.py` (our own provider, never the global one), carry names, counts, ids and an error's class only (`record_exception` off), and a queued job carries its context (`queue.encode_job`). Never send the LiteLLM proxy `trace_id` metadata (RUNTIME_PATTERNS.md 113).

## Metrics and alerts

- Add counters/histograms in `app/core/metrics.py` using its `.labels(...).inc()` surface;
  histograms get explicit buckets, not SDK defaults. Telemetry is configured at process start
  (`configure_telemetry`), never at import time.
- Every degrade-and-continue path increments a metric. If the failure can leave a human unaware of
  committed business state (failed notify, degraded dedup, failed upload), also add an alert
  rule to `observability/prometheus/alerts.yml` — a metric nobody alerts on is still silent.
- Caller-facing errors use the `ErrorCode` envelope (`app/core/errors.py`); a tool's message to
  the LLM stays natural language (`friendly_tool_error`).
