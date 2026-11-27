# Feature Specification: Distributed tracing, with one id shared with Langfuse

**Feature Branch**: `feat/distributed-tracing`

**Created**: 2026-11-27

**Status**: Implemented. Verified hermetically and with the repository's collector and Tempo configs in throwaway containers (below); not run on the live stack or deployed.

**Input**: "I only see x-request-id, not a tracing id?" Then: build real tracing, and make sure it is not confused or mixed with Langfuse's trace id.

## Why

A turn crosses the HTTP API, a Redis stream, a worker, a graph of about a dozen nodes and several model and tool calls. Logs (RUNTIME_PATTERNS.md 112) answer *what happened to this request*; nothing answered *where its time went*. Langfuse records what the model saw, not the queue wait or the node timings, and gave its trace a random id that appeared in no log.

## Decisions

| Question | Decision | Why |
|---|---|---|
| How many "trace ids" | **One.** The W3C/OpenTelemetry id is what `trace_id` means in a log line, in `X-Trace-Id`, in Tempo and in Langfuse | Two ids with one name is the confusion to avoid; Langfuse accepts a caller's id and its v3 SDK uses the OpenTelemetry id itself |
| Several graph runs in one request (an unattended turn declining an approval) | One Langfuse trace; only the first run sends `name` and `input` | Langfuse upserts on id and keeps fields not sent (checked against the running v2 server) |
| A job run again after a crash | A new trace, with a link to the first | Two attempts must not be written into one record |
| An approval resume | A new request, so a new trace; `thread_id` joins them | It may come hours later |
| A client's `traceparent` | Ignored; the API always starts the trace | A caller who chose the id could write into another request's Langfuse trace |
| The LiteLLM proxy's own Langfuse traces | Left separate. The app never sends the proxy `trace_id` or `existing_trace_id` metadata | The proxy reads both (pinned image) and would merge its generations into the app's trace, counting every call twice |
| The tracer | Our own `TracerProvider`, not the global one | `mcp`, `langsmith` and `litellm` emit spans of their own whose contents we do not control |
| Instrumentation | By hand at the seams; no auto-instrumentation | No new dependency, and no library records a URL, SQL text or Redis argument |
| Span content | Names, timings, token counts, ids, error CLASS. Never text, arguments, results, exception messages or the principal | Constitution V: metadata only |
| Backend | Tempo, single binary, 7 days (the logs' retention) | Same vendor as Loki and Grafana, which link the two |
| Sampling | `OTEL_TRACES_SAMPLE_RATIO`, 1.0, decided at the API and followed by the worker | A trace is exported whole or not at all |

## What a trace holds

`POST /chat/stream/queued` → `agent_job turn` (worker) → one `node <name>` per graph node → `chat <alias>` (token counts) and `execute_tool <name>`. An ingest upload continues into `ingest_job`.

## Success criteria

- One request's spans share one trace id across the API and worker, and every log line of it carries that id (`tests/core/test_distributed_tracing.py`).
- No question, argument, answer or principal appears in any span (same test).
- With the repository's `otel-collector` and `tempo` configs, a turn exported over OTLP is one tree of 17 spans in two services, opens in the provisioned Grafana datasource and offers its logs.

## Not included

- The front door: Caddy and `authlimit` start no span, so a trace begins at the API.
- Outbound HTTP: no span or `traceparent` for LiteLLM, the ML service, Qdrant, Postgres or Redis. Their time is inside the node that called them.
- The MCP gateway, the Telegram bot and scheduled jobs.
- Alerts on a failing exporter. Spans are best-effort and dropped, like metrics.
