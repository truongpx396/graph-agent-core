# LangGraph patterns

The numbered patterns the agent in `app/agent/` applies, each with the bug or gotcha behind it. Numbers are stable: code and tests cite them ("pattern 43"), and a number is never reused or moved. **Bug** marks a real defect that was found and fixed; **Gap** marks something disclosed and left open. The long evidence behind a cost or performance fix lives in the work order it names ([PRODUCTION_REVIEW.md](PRODUCTION_REVIEW.md), R01 to R35), not here.

**Citing.** A bare number is a pattern in this file. Patterns that are not about the graph moved out and left their numbers behind (see [51-66](#51-66-moved)). Three numbers were also used twice by mistake and cannot be renumbered, so cite the other one with its file: 74 ([COST_GOVERNANCE.md](COST_GOVERNANCE.md), the gateway's view of spend), 86 and 87 ([ENGINEERING_PATTERNS.md](ENGINEERING_PATTERNS.md), smoke checks and live tests).

| Concern | Patterns |
|---|---|
| Agent loop | 1–7, 9, 10, 34, 35, 39, 70, 71, 81 |
| Approval and governance | 8, 15, 36, 80, 87, 92 |
| Identity and tenant security | 12, 17, 25, 30, 32, 78, 82, 84–86, 91, 94, 97 |
| Retrieval, memory, caching | 2, 13, 18–20, 22, 24, 33, 41, 67, 79 |
| Models and routing | 38, 68, 73, 74 |
| Tools and data | 21, 27, 28, 31, 45, 46, 50, 78, 82, 87 |
| Reliability and scaling | 16, 43, 49, 89, 93, 96, 98, 99, [exactly-once](#extending-further) |
| Observability | 11, 14, 37, 38 |
| Interfaces and extensibility | 23, 29, 42, 44, 47, 89, 91–94 |
| Quality gates | 40, 48 |
| Cost: pricing, the usage meter, the spend limits | [COST_GOVERNANCE.md](COST_GOVERNANCE.md) |

## Patterns

### 1. Input validation with a real exit
`validate_input` stamps state; `graph_edge_route_after_validate_input` sends bad input to `reject_input` (an `AIMessage`: the system speaking, not the user) and on to `END`. Valid input continues.
- **Use when:** always. It costs nothing and catches most bad cases.

### 2. Context enrichment
`retrieve_context` pre-fetches before the LLM reasons. `agent` carries the result as text ahead of the turn's question, in the same message (79), never as a system message. The domain's system prompt is added by the agent node at call time (19); nothing stores it in the thread.
- **Use when:** you have a knowledge base (Qdrant, a DB, an API).

### 3. State tracking
Flow control lives in explicit state (`iterations`, `context`, ...), which makes loop limits, retries and routing possible.
- **Bug:** `context` and its anchor are written only by `retrieve_context`, which a cache hit skips (22). A cache-hit answer that failed `check_output` was retried with the PREVIOUS turn's context. `validate_input` now resets both each turn (`TestPerTurnReset`).

### 4. Conditional routing
`graph_edge_route_after_agent` is an edge function, not a node: it decides where execution goes after `agent` (tools, approval, budget exit, or done).

### 5. Output gate with a real retry
`check_output`, `graph_edge_route_after_check_output` and `retry_output`. A rejected answer gets a corrective `HumanMessage` and loops to `agent`; `MAX_ITERATIONS` bounds it.
- **One registry, not a flag per check.** Each reason is an `OutputCheck` in `graph/output_checks.py` (code, detector, feedback, metric, whether an exhausted answer is still safe to show, fallback text) in one ordered tuple whose order is the priority. `check_output` records what fired in `State.output_findings`; a new check is one entry.
- **Retry artifacts.** The feedback is a `HumanMessage` only because that is the role a model acts on; the person did not say it. `retry_output` tags it, and the answer it rejected, with `RETRY_ARTIFACT_KEY` (`graph/messages.py`). Every reader that looks for "the user's message" uses `is_user_message`; the agent node drops earlier turns' artifacts; an `AIMessage` with `tool_calls` is never tagged (it would orphan its `ToolMessage`s).
- **Bug:** the feedback was a plain `HumanMessage`, so after one retry the cache was keyed by the feedback, a vague follow-up searched on it, and a turn that ran an approved write then retried **was written to the semantic cache** (breaking 22). Test: `TestRetryFeedbackIsNotConversation`.
- **Cost:** many checks exist for 3B/7B models. `agent_output_check_fired_total{check, tier}` counts which fire per model tier, and `OUTPUT_CHECKS_OFF_ON_ROUTED_TIERS` (empty by default) can switch checks off on medium and strong turns, never `leaked_prompt` or `fabricated`. Which to list is the owner's call, from the counts (R32).

### 6. Loop control
`MAX_ITERATIONS` is the shared cap for the tool loop and the output-retry loop.

### 7. Error recovery
`ToolNode(handle_tool_errors=friendly_tool_error)` turns a tool exception into a `ToolMessage` the agent sees. Three failures, three policies: a tool fails mid-turn, recover via that message; `retrieve_context` fails, **degrade** to no pre-fetch; the `agent` call fails, **retry** (`AGENT_RETRY_POLICY`, which excludes programming errors and, since 71, permanent client errors).
- **Bug:** three layers retried a model call and multiplied (SDK x graph policy x gateway = 27 calls to the provider in an outage), and `ChatOpenAI` had no HTTP timeout. Now the SDK retries nothing (`LLM_SDK_MAX_RETRIES=0`), the read timeout is `LLM_READ_TIMEOUT_SECONDS`, connect and pool are 5 s, and the graph keeps the one retry that knows what must not be repeated (R06).
- **Bug:** a stream that broke part-way and was retried showed the first attempt's tokens before the whole answer. `_run_graph_stream` treats a model call that starts while the previous one is open and had sent tokens as a restart: it clears its answer, counts `agent_stream_restarted_total` and sends the `retry` event.

### 8. Human-in-the-loop
`human_approval` calls `interrupt()`; a caller resumes with `Command(resume=True/False)`. Two routes reach it: the opt-in `require_approval` flag and the mandatory route for any non-`read_only` call (15). `make chat-hitl` is a runnable example.
- **Gotcha:** a rejection still needs one `ToolMessage` per pending `tool_call`, or the next LLM call fails. `human_approval` synthesizes them.
- **Unattended callers** auto-decline with `Command(resume=False)`, up to `UNATTENDED_MAX_DECLINE_ROUNDS` (3), then cancel the run with one explicit "needs a person's approval" message.
- **Bug:** a rejection's `ToolMessage` had no `name`, so a skill's required tool still counted as unsatisfied and every later round demanded it. `reject_tool_calls` now sets `name` on every path.
- **Bug:** declining only once left a re-requested write paused: an empty reply, then every message refused as "pending approval".
- **Use when:** an action sends, writes, spends or deletes.

### 9. Parallel tool execution
`ToolNode` already runs several calls from one LLM turn concurrently; no fan-out node is needed.

### 10. Layered safety budgets
Each budget catches a failure the others cannot.

| Budget | Where | Catches |
|---|---|---|
| `MAX_ITERATIONS` | `graph_edge_route_after_agent` | Model stuck calling tools |
| `MAX_TOOL_CALLS_PER_TURN` | `graph_edge_route_after_agent` | One turn fanning into dozens of calls |
| `MAX_TOKENS_PER_TURN` | `graph_edge_route_after_agent`, `agent()` | Unbounded spend over few iterations |
| `HISTORY_TOKEN_CEILING`/`FLOOR` | `compact_history` | History growing without bound (13, 41) |
| `MAX_COST_USD_PER_TURN` | `graph_edge_route_after_agent`, `agent()` | Same tokens, different dollars by tier (35) |
| `MAX_REPEATED_ACTIONS` | `graph_edge_route_after_agent` | The same call with the same args, repeatedly (34) |
| `TOOL_TIMEOUT_SECONDS` | Per tool call | A hung Qdrant or embedding call |
| `REQUEST_TIMEOUT_SECONDS` | Whole turn | Many slow-but-not-hung steps |
| `RECURSION_LIMIT` = `MAX_ITERATIONS * 2 + 15` | LangGraph step cap | A routing bug causing a node cycle |

- **Bug:** `iterations` and `total_tokens` persist across a thread, so `validate_input` resets them each turn (not on a resume). A reset also needs a reducer that allows it: `subagent_spend` used `operator.add`, so "reset to `[]`" did nothing. Assert on state read back across two turns (`TestPerTurnResetThroughTheGraph`).
- **Bug:** the safety-net exits routed to `no_answer`, which never answered the `tool_calls` of the message that tripped them, so the next LLM call was rejected. `no_answer_fallback` now answers each pending call and treats text beside a tool request as untrusted; `run_subagent_impl` reports such a run `budget_exceeded`, not `completed`.
- **Use when:** any agent talking to a real model and tools. The shape (several narrow budgets) matters more than the numbers.

### 11. Custom metrics
OpenTelemetry counters and histograms (`app/core/metrics.py`) for the "how often, across everyone" view that Langfuse's per-call traces cannot give. Every process pushes OTLP to one collector, so one scrape target covers the API and all workers. Histogram buckets are explicit (the SDK default puts every 1-90 s turn in one bucket).
- **Bug:** scheduled jobs exited before the 15 s export timer and exported nothing, so a failing digest could never fire its alert. Each now runs in `job_runtime.py::scheduled_job()`, which flushes in a `finally`.
- **Bug:** a Langfuse client per turn started three threads nothing stopped. Now one shared client per process (`app/core/tracing.py`), and its exit flush runs on a daemon thread bounded by `LANGFUSE_SHUTDOWN_TIMEOUT_SECONDS` (it once hung ~90 s with Langfuse unreachable).
- **Gap:** a SIGKILL loses unflushed metrics.

### 12. Untrusted content framing
Retrieved text is wrapped in `<retrieved_document>` delimiters and the prompt says delimited content is data, not instructions. One function frames everything: `app/core/untrusted.py::frame_untrusted` (the pre-fetch, the page-reading tools, replayed CRM notes).
- **Bug:** only the pre-fetch wrapped anything; page readers returned raw text, one enrichment stored 20,000 characters that a read-only tool replayed unframed, and a page containing `</retrieved_document>` closed the frame early. Tag-shaped text inside is now neutralised and notes are framed and bounded at replay.
- **Limit:** it cannot stop a model that follows what is inside; the approval gate does (Principle II).

### 13. Bounded conversation history
`messages` is the one `State` field with no cap by construction. `compact_history` trims with `RemoveMessage` by estimated tokens with hysteresis: a no-op at or under `HISTORY_TOKEN_CEILING`, otherwise it drops the oldest whole turns down to `HISTORY_TOKEN_FLOOR` (trimming to the same ceiling would re-trigger every turn and break the provider's prefix cache). It is turn-aware, so a `tool_call`/`ToolMessage` pair is never split, and dropped turns fold into a summary (41).
- **Cost:** a turn's old tool results (a 20,000-character page, a sandbox output) were re-sent on every later call. `without_stale_tool_results` replaces results from earlier turns longer than `STALE_TOOL_RESULT_MAX_CHARS` (2,000; 0 = off) with a short note, **in the copy sent to the model only**; the stored thread keeps the full text and every call keeps its answer. It trades one prompt-cache miss per turn for a much shorter prompt (R31).

### 14. Node telemetry
`build_graph` wraps every node (never inside the node) in `instrumented`: `node_started`, then `node_completed`, `node_failed` or `node_paused`, with the node name, a per-turn `run_id` and `duration_ms`. `human_approval`'s `GraphInterrupt` is a pause, re-raised untouched.
- **Never logged:** message content or the `state` dict. An unexpected failure logs where, not what: the class and `origin` (file, line, function), never the message, which can carry a host, SQL or a DSN (30).

### 15. Tool capability declarations: a mandatory gate
Every tool declares `read_only`, `mutating` or `outward` (`SHARED_TOOL_CAPABILITIES` in `app/agent/tools/builtin.py`, merged with each domain's own). `graph_edge_route_after_agent` sends any pending batch with a non-`read_only` call through `human_approval` **unconditionally**; a tool missing from the map defaults to `outward`. No flag turns this off.
- **Why mandatory:** a RAG agent carries untrusted content on nearly every turn (12), so a write capability is one gamble from a document steering a real write.
- **`add_note` is the worked example:** a write tool is a fixed, typed, closed-vocabulary operation whose point id is derived by code, never supplied by the model.
- **Use when:** the moment a second tool exists. Retrofitting after several writers accumulate means auditing them all at once.

### 16. Durable checkpointing and cross-restart compatibility
`build_graph()` defaults to `MemorySaver` for tests; the runtime uses `AsyncPostgresSaver` (its own database), so a paused approval survives a restart and several processes share one store. Every process awaits `init_graph_async()` on its own loop, because the saver's lock binds to the loop that created it.
- **`STATE_SCHEMA_VERSION` + `graph_version`:** each turn stamps both. `resumability_error_async` runs before every resume and separates `checkpoint_lost` (no paused run) from `checkpoint_incompatible` (schema mismatch). A differing build SHA alone is not an error.
- **A topology change can hide a mid-flight turn.** LangGraph names the channel that triggers a node after the edge feeding it, and a conditional edge's channel carries its router's name. Renaming a router or removing a node left a checkpoint with `next == ()`, so a resume ran nothing, silently (`test_checkpoint_across_a_topology_change.py` pins it). `build_graph` now registers each router under `EDGE_CHANNEL_NAMES` (see Naming), so only changing that table is a topology change. A paused-at-approval turn is unaffected while `human_approval`'s edges are unchanged.
- **Deploying a graph change: stop the workers and let in-flight turns finish first.** A turn that ends with nothing left to run is dead-lettered (`nothing_to_continue`).

### 17. Multi-tenant isolation
A `SecurityCtx` (`tenant`, `principal`, `claims`) is stamped once by `validate_input` from `config["configurable"]["ctx"]`, never from message content. `Policy.lower(ctx, target)` becomes a Qdrant filter applied **inside the query**, never a Python post-filter (a buggy one looks identical to a correct one until a cross-tenant leak). `documents` scope to `tenant`; `memories` also to `owner`. Every ctx-aware tool re-checks the ctx (fail closed).
- **Where the ctx comes from:** the API builds it from the identity the front door signed (86), or from `X-Tenant-Id`/`X-Principal-Id` only in `AUTH_MODE=headers`, which is for the tests and a local API alone.
- **Bug:** thread state was keyed by `thread_id` alone and only the `GET` routes checked ownership, so anyone naming an id could read it, approve its pending action or cancel it. `_require_conversation_owner` now runs first on send, resume and cancel; every refusal is the 404 an unknown id gets.
- **Gap:** the check is at the API, so anything publishing to Redis directly bypasses it (89 closes this for the channels).
- **Use when:** more than one customer shares a deployment. Do this first.

### 18. Cross-session memory
`remember` is the only way a memory is written (`mutating`, gated like `add_note`). Recall is automatic: the model decides to *write*, never to *read*. The memory leg of `gather_context` re-applies `Policy.lower` on every call, scoped to tenant and owner, never cached. Memories are framed like retrieved documents: a poisoned one replays every turn until removed.
- **Not built:** an LLM-facing forget tool (`delete_by_filter` serves a data-subject-request script, not the model).

### 19. Prompt-cache stability
`SYSTEM_PROMPT` is a plain constant: no principal, tenant or timestamp is ever interpolated, and the `SecurityCtx` never enters the messages sent to the LLM (`test_prompt_cache_stability.py` asserts byte-identical rendering across two ctx values).
- **Bug:** the prompt used to be seeded into each thread once, so every edit appended a second prompt after the old one, and an unseeded graph (a whole test suite) ran as an un-guided agent. The prompt is now **not stored at all**: the agent node prepends `manifest.system_prompt` on every call, so an edit reaches every conversation with nothing to migrate (`test_system_prompt_at_call_time.py`).

### 20. Hybrid retrieval, rerank and cited answers
`hybrid_search()` (`qdrant_store.py`) runs two Qdrant legs (dense via LiteLLM, BM25 sparse via in-process `fastembed`) fused with RRF, then reranks with a cross-encoder in the `ml-service` container. Every hit is numbered `[1]`, `[2]`, ...; `check_output` scans the answer for the markers it actually contains and writes only those to state, **never trusting the model's claim about what it cited**.
- **Degrades in two layers:** sparse failure falls back to dense-only, reranker failure returns the RRF order; the answer is still cited.
- **Performance fixes (each a work order):** one Qdrant client per event loop, not one per call (R03); payload indexes on `tenant`, `kind`, `owner`, `topic`, `source`, `created_at` (an existing deployment runs `make qdrant-indexes` once, R09), with a real-Qdrant tenant-isolation test; one shared ML-service HTTP client per loop and a 300 s `embed_query` cache that shares concurrent requests (R10); retrieval and the router run as one parallel step, and `AgentManifest.prefetch="never"` leaves `retrieve_context` out of a domain's graph (none does today; R29).
- **Use when:** retrieval quality matters and an answer needs an audit trail.

### 21. Fixed-tool structured data access, plus MCP exposure
`query_employees` is a closed, typed Postgres query: `tenant` (from `SecurityCtx`), a `department` enum and `name_contains` are the only variables, every query is parameterized and ANDs `WHERE tenant = %s`. There is no `execute(sql)`. The access boundary lives in reviewable code, not in a string the model writes. The MCP server wraps the same query under a separate trust boundary (78).
- **Use when:** HR lookups, order status, inventory counts, wherever text-to-SQL is tempting and exactly wrong.

### 22. Semantic cache
`check_semantic_cache` does a cosine-KNN lookup in Redis Stack scoped to tenant, principal **and domain**. A hit at or above `SEMANTIC_CACHE_SIMILARITY_THRESHOLD` (0.95) skips retrieval and the LLM and rejoins at `check_output`; `write_semantic_cache` stores confirmed-final turns. Every cache call degrades to a miss: a latency optimization, never a correctness dependency (`app/adapters/outbound/redis/semantic_cache.py`).
- **Bug:** it cached turns that acted: after an approved write, a near-identical request got "Ticket #123 created" with no model call, approval or write. The write node now skips any turn that called a non-`read_only` tool.
- **Bug:** tag escaping. RediSearch treats `-` as syntax, and a list of known characters missed `|`: principal `alice|bob` also matched `bob`'s entries. `_escape_tag` now escapes every non-alphanumeric ASCII character.
- **Bug:** the cache ignored the domain, so another domain handed the first one's answer. The domain is now a TAG field and a required keyword of the `AnswerCache` port, and the index carries a schema version (R23).
- **Bug:** a follow-up ("yes", "do it", "and for March?") matched whatever similar short message another conversation had cached. The cache is now read and written only for a conversation's **first** question (R33). `SEMANTIC_CACHE_ENABLED=false` turns both nodes into no-ops. After a week of `agent_semantic_cache_total`, turn the cache off if hits are rare.
- **Not covered:** an answer quoting live read-only data can be stale until its TTL.

### 23. Config-first multi-domain composition
`build_graph(manifest, domain)` keeps its topology and adapts through an `AgentManifest` (config: name, prompt, exposed tools) and a `DomainPlugin` (code: tools, capabilities, policy). There is no `if domain == "..."` and no default domain; Ecorp is one more entry in `app/domains/registry.py`. A domain's capability map is bound to the router with `functools.partial` once per build.
- **The load-bearing test:** `tests/agent/test_manifest.py` builds a domain with a different `Policy` and an Ecorp-unknown tool and proves the unmodified gate treats it as `mutating` using that domain's map.
- **Bug:** Ecorp's prompt and tools were welded into the graph and five functions defaulted to it, forcing deferred imports and letting any module pick up one domain's tools. `graph/build.py` is now the only module that imports every node.

### 24. General-purpose ingestor with parent-child chunking
`ingest_text`/`ingest_file`/`ingest_url` and `make ingest` share one chunk, embed, `build_point` pipeline (`app/ingestion/`); every item carries its tenant and is refused without one. Parents (~1200 chars) go to the prompt, overlapping children (~600) are embedded. Point ids derive from `(tenant, source, chunk index, content)`, so re-ingesting identical content upserts.
- **SSRF guard:** `https` only, every resolved address globally routable, no redirects, async resolution.
- **Bug:** the first guard was a deny-list and missed `100.64.0.0/10` (where cloud metadata lives). The fetcher also checked a host's addresses and then let the HTTP client resolve again (a rebinding window), and read the whole body before the size check. It now connects to an address it checked, with the real host as `Host` and TLS name, and streams the body, stopping at the limit (R20).
- **Bug:** re-uploading an EDITED document left the old chunks retrievable and citable. An ingest with an `ingest_id` writes its own id space, then removes the tenant's chunks of that source from strictly OLDER ingests (`delete_older_versions`). "Delete whatever has a different ingest id" was wrong: two simultaneous uploads deleted each other and left no document. `DELETE /ingest/documents` removes a document's chunks and stored files (R18).
- **Gaps:** an older upload that finishes after a newer one's cleanup keeps its chunks until the next upload; the semantic cache may quote old text for its TTL; nothing lists a tenant's documents; uploaded blobs are never removed after ingest except by that delete. The **crawler's** rebinding window is not closed: its containment is the network (`deploy/README.md`, "The crawler's network").

### 25. Input moderation before any spend
`moderate_input` (`app/core/moderation.py`) runs right after `validate_input`, before the cache, compaction, retrieval or any LLM call: (1) a regex screen for known injection phrasings plus a small denylist, (2) Llama Prompt Guard 2 served by `ml-service`'s `/prompt-guard` (`ML_INJECTION_THRESHOLD` 0.5, ~25-100 ms). A hit fails **closed**; a failure of the check itself fails **open** and is recorded `outcome="error"`.
- **Bug:** compaction (an LLM call) ran before moderation and the cache, so refused and cached turns still paid for it. The order is `validate_input`, `moderate_input`, `check_semantic_cache`, then on a miss `compact_history` and `retrieve_context`.
- **Gap:** moderation screens words, not pixels (44), and the threshold is not recalibrated on live traffic.
- **Use when:** any input surface reachable by someone not fully trusted.

### 26. (moved)
Moved to [COST_GOVERNANCE.md](COST_GOVERNANCE.md): the usage meter and spend limits are not graph patterns. The number is kept so references resolve.

### 27. Clarification and follow-ups without forking the graph
`ask_clarification` is an ordinary `read_only` tool whose result the prompt says to relay verbatim. Follow-ups are a real node, `suggest_followups`, after `check_output`'s non-retry branch: one small LLM call for 2-3 questions, run in parallel with the cache write (22) so `done` waits for the slower, not the sum. They gate on `used_citations` (an answer with nothing derived has none) and are skipped on a cache hit.
- **Cost:** that second model call held back `done` on every cited answer. `AgentManifest.suggest_followups` (default `True`) and `FOLLOWUPS_ENABLED` decide at build time whether the node exists; `ops`, `support` and `sales`, whose answers end in an action, turn it off. Emitting `done` first was rejected: it is terminal for every reader of the stream (R30).

### 28. Consuming a remote MCP tool catalog
`load_remote_tools(...)` (`app/adapters/outbound/mcp/client.py`) connects to an MCP server and wraps each tool as a `StructuredTool` with the remote's JSON Schema.
- **`capability_overrides` is the only source of truth.** MCP annotations are self-reported hints; an unnamed tool defaults to `outward`, so the mandatory gate applies with no special case.
- **Async only, one shared connection per server and loop** (`connection.py`, 78). What the remote says is input, not fact (78).

### 29. Built-in web UI
One self-contained HTML file (`app/adapters/inbound/api/static/index.html`, no build step, no CDN) at `GET /`, talking only to `POST /chat/stream/queued` and rendering the SSE vocabulary. It uses `fetch()`, not `EventSource`, because the endpoint is POST and needs identity headers.
- **Bug:** against a live `uvicorn`, sync checkpointer calls raised `asyncio.InvalidStateError` on the loop the async saver was opened on; three call sites became `aupdate_state`/`aget_state` and the `MemorySaver` suite could not have caught it.
- **Bug:** a transcript was read whole. `GET .../messages` takes `limit` (1-500) and `before`, each message carries its `index`, and the page loads the last 50 with "Load earlier messages". This bounds what is sent, not what is read (R27).

### 30. Canonical error envelope
One shape, `{code, message, details}`, from a single `ErrorCode` enum (`app/core/errors.py`) for every SSE `error` event and CLI error; not applied to `ToolMessage` content, which is prose for the model. `internal_error_envelope(exc)` returns a fixed message plus `error_class`, never `str(exc)`.
- **Bug:** both catch-alls sent `str(exc)` to the caller, leaking hosts, SQL fragments and DSNs; three tests asserted the leak. Two more error events (no worker answered, a refused resume) carried no `code` (R24).
- **Gap:** the ingest worker's `process_job` mixes uploader-facing messages with unexpected failures and needs a per-exception decision.

### 31. DB connection pool
One lazy `psycopg_pool.ConnectionPool` singleton per database behind `get_connection()` (`app/platform/sql_store.py`); `close_pool()` runs at API shutdown, since background pool threads outlived the process. Pools replace their dead connections after the first failure (ENGINEERING_PATTERNS.md 102).

### 32. Credential scrubbing on tool output
`app/core/scrubbing.py`, at the one chokepoint every tool result passes through: static patterns for credential shapes (API keys, AWS ids, `password=`, `user:password@`, JWTs) plus this deployment's own configured secret values. Tool output specifically, because a raw DB row can carry a credential into `ToolMessage.content` without passing through the model's input. Fails open (logged).

### 33. Memory deletion audit, age selector, retention at recall
`delete_memories` requires **exactly one** selector (an ambiguous request is refused, not narrowed to "everything"), counts matches with the same filter first and records every call. `Policy.lower`'s memory filter also ANDs a `DatetimeRange` (`MEMORY_RETENTION_DAYS`, 365) onto every recall, so an unswept expired memory is invisible.
- **Gap:** nothing calls `delete_memories` (no script, endpoint or make target); retention is enforced at read time only.

### 34. No-progress detection
`consecutive_repeat_count` scans back from the latest `HumanMessage`; `_tool_call_fingerprint` normalizes a batch order-independently. `MAX_REPEATED_ACTIONS` (3) identical batches end the turn. It is a pure function of `state["messages"]`, so no second counter can drift.

### 35. Cost ceiling
`MAX_COST_USD_PER_TURN` is a hard stop on cumulative per-turn dollars, distinct from the token cap because the same tokens cost differently by tier. `agent()` prices each call from LiteLLM's rates (`pricing.price_usage`) and checks **before** the next call: enforcement, not just recording ([COST_GOVERNANCE.md](COST_GOVERNANCE.md)).

### 36. Run cancellation
A third outcome for a paused interrupt, not a repurposed rejection: `graph_edge_route_after_human_approval` checks `cancelled` first and goes to `__end__`, never back to `agent`. A rejection lets the agent react; a cancel must stop the run.
- **Bug:** nothing cleared `cancelled`, so a later approved pause on the thread went straight to `__end__`, leaving a dangling `tool_call`. `validate_input` resets it each turn.
- **Bug:** `astream_events_resume` took no `cancel_check`, so a cancel between "approve" and "done" set a flag nothing polled. Cancellation is cooperative: a running tool finishes.
- **Bug:** a stream closed before its terminal event (a consumer that stopped iterating) left a blank trace and no metrics. `GeneratorExit` now finishes the trace and counts `agent_requests_total{outcome="abandoned"}` (alert `TurnsAbandoned`); every loop over these streams uses `contextlib.aclosing`, because a bare `async for` does not close the generator when left. `process_request` always acks, so an abandoned turn is never retried.
- **Structure:** the three entry points share `_BudgetHold` and `_run_turn` (R35 B), and the runtime no longer recognises nodes by name: a node announces its own `retry`/`compacted` event with `adispatch_custom_event`, and one branch forwards every announcement not tagged `metadata.subagent_name` (R35 A).

### 37. Per-tool-call audit record
`MetricsCallbackHandler` correlates a `tool_called` start log with its outcome by LangChain's `run_id`. Args and results are logged as a truncated SHA-256 fingerprint, never raw content, so "which tool ran, with what, did it succeed" is answerable at 3 a.m.

### 38. Resolved model recording
`resolve_model` (`app/adapters/outbound/litellm/model_catalog.py`) asks LiteLLM's `GET /model/info` which concrete model serves an alias, cached per process, and records it as `resolved_model` on each usage event, so a silent remap behind an alias is visible.
- **Bug:** the first version was a synchronous `httpx.get` on the event loop at the end of every turn, freezing every other turn on that worker and paying a fresh 5 s timeout per turn in an outage. It is `async` and remembers a failure for 60 s per alias.

### 39. Ungrounded-claims count
`count_ungrounded_claims` mirrors `find_used_citations` (20) but is computed **independently** from the answer text, so a bug in one cannot mask the other. It counts `[n]` markers matching no returned citation, making "did the model cite something real" a gateable property (40).

### 40. Eval statistical rigor
`scripts/checks/eval.py` has two gates: each case runs `EVAL_REPETITIONS` (5) times and passes only if `REPETITION_PASS_THRESHOLD` (80%) pass (a stochastic system graded once is a coin flip), and a grounded-claims gate over the whole set (at least 95% of markers real), computed from the runtime's own counts, never a model's opinion of itself. The thresholds are demo defaults.

### 41. Bounded history summarization
`compact_history` runs once per turn that reaches the model, after moderation and a cache miss. It summarizes exactly the discarded messages with one LLM call into a cumulative `state["history_summary"]`, which `agent()` puts ahead of the question (79) with a short "don't restate this" reminder.
- **Bug:** the summary and context landed after the question, because the anchor was `len(messages) - 1`, which was the compaction breadcrumb. The anchor is now the question's own index (`test_compaction_anchor.py`).
- A cache hit skips it, so a long run of hits defers compaction to the next miss (accepted: it takes hundreds of hits). A summary over `MAX_HISTORY_SUMMARY_CHARS` routes to `context_window_exceeded`, a named dead end, not silent truncation.

### 42. Telegram channel
`app/adapters/inbound/channels/telegram/` resolves a stable conversation and person per chat and runs each message as a turn through the front door (89), then collects the events into one reply. It reaches the public internet by design; `TELEGRAM_BOT_TOKEN` is empty by default and `run()` refuses to start without one. Approvals are buttons (92), updates are acknowledged when durable (93) and accounts link by the person's own login (94).
- **Bug:** the long-poll `offset` was a local variable, so a restart redelivered every update Telegram remembered, each a duplicate turn. It is durable in Redis (`telegram:offset:{domain}`) and saved **after** handling.
- **Bug:** a turn with no text produced no message, which read as the bot ignoring the user; `handle_message` substitutes a fallback.

### 43. Redis Streams queue between the API and agent workers
The only HTTP chat path. `POST /chat/stream/queued` publishes a turn to a Redis stream and relays a per-request results stream as SSE; it never runs the graph. `agent_worker.py` reads through a consumer group, so **N workers is the scaling story**: the SSE tier scales connections, the worker tier scales turns. Delivery is at-least-once: ACK only after `process_request` finishes, and a crashed worker's job is reclaimed (see Extending Further).
- **The per-thread lock belongs to the worker**, not the HTTP path, so every front door gets it (89).
- **Bug:** redis-py's async `socket_timeout` (5 s) raced `XREAD`'s server-side `BLOCK`; use `socket_timeout=None`.
- **Bug:** the requests stream only grew: `XACK` leaves the entry in the stream, and every message with its image data URIs stayed in a 256 MB Redis. Every ack is `streams.py::ack_and_delete` (`XACK` and `XDEL` in one MULTI), a reclaim pass trims by age with `MINID`, and **never `XADD ... MAXLEN`**, which deletes jobs nobody has run (R04).
- **Bug:** a Redis blip ended the worker and every turn in flight. A failed READ (`redis_client.UNAVAILABLE_ERRORS`) is retried with backoff and counted (alert `AgentQueueReadFailing`); a write is never retried and redis-py's client-wide `retry=` stays off, since it would re-send an `XADD` the server had applied and run a second turn (R07).
- **Bug:** nothing bounded a request. `message` is at most `CHAT_MESSAGE_MAX_CHARS` (32,000), `thread_id` 1-200 characters, `images` at most `CHAT_MAX_IMAGES` of `CHAT_IMAGE_MAX_CHARS`; `deploy/caddy/gateway.caddy` is the outer bound (R14).
- **Bug:** a hung worker was never restarted. Workers touch `worker-heartbeat-<queue>-<loop>` files that the container health check reads (`python -m app.platform.worker_heartbeat`), and `AgentWorkerLoopStalled` fires. Compose does not restart an unhealthy container (`deploy/README.md` says what does) (R22).
- **Cost:** three Redis round trips per token became one pipelined `MULTI`; step 2 (batching tokens) is not built (R08).
- **Visibility:** `agent_queue_lag`, `agent_queue_pending`, `agent_queue_wait_seconds` and `agent_first_token_seconds` size the worker pool; alerts `AgentQueueBacklog`, `AgentQueueWaitHigh`, `WorkerUnreachable`. One trace per turn across the queue is not built (R17 part 2).
- **Resumable reads:** each SSE frame has an `id:`, `X-Request-Id` names the job, a quiet stream sends `: keepalive`, and `GET /chat/stream/{request_id}` with `Last-Event-ID` reads on from there, owner-checked (R13). The web page and CLI do not reconnect on their own yet.
- **Not done:** a consumer-lag gauge, a readiness probe for consumers, pruning the consumer names of crashed workers.
- **Use when:** SSE connection count and LLM concurrency must scale on different axes.

### 44. Multimodal image questions
`_build_human_content(text, images)` returns a plain string with no image and an OpenAI/LiteLLM content list with one or more; the app never fetches or decodes an image. Five call sites assumed `.content` was a string, so `human_text`/`human_has_content` handle both, and an image-only question is not rejected as empty.
- **Scope:** moderation screens words, not pixels; retrieval and caching stay text-only. Verify vision **and** tool-calling support before assuming an alias swap works.

### 45. Skill packages with progressive disclosure
A catalog of `SKILL.md` packages (YAML frontmatter + markdown, in a domain's `skills/` folder) found by meaning. `skill_search` hybrid-searches `{name, description}`; `use_skill(name)` loads one body, so a turn that needs no skill pays for two tool schemas, not the catalog (`app/agent/tools/skill_catalog.py`). **Disk is content truth, Qdrant only the index.** The folder a skill sits in scopes it; there is no `domains:` field, and no skill is shared across domains today.

### 46. Subagents: scoped, isolated delegation
`run_subagent(subagent_name, task)` runs a separate nested graph (`build_subagent_graph()`, `SUBAGENT_PROFILE`) with its own messages, prompt and budget (`MAX_SUBAGENT_ITERATIONS=6`, `MAX_SUBAGENT_TOKENS_PER_RUN=4000`), built from `app/domains/<domain>/subagents/<name>/AGENT.md`. Skills add instructions to the same context; this does not.
- **Read-only tools only**, enforced at catalog-build time, on a throwaway `MemorySaver`: nobody could resume it hours later, so a write-capable subagent would deadlock or bypass the gate (15). No recursion: `run_subagent` is stripped from a subagent's tools.
- **Isolation:** inherited `SecurityCtx`, nested spend folded into the parent's budget through the `subagent_spend` reducer, its reasoning never reaches the client's stream.
- **Citations are the parent's alone:** the nested graph is built with `enforce_citations=False` and `run_subagent_impl` strips stray `[n]` markers (a heuristic: "pick element [2]" reads as one).
- **Bug:** the nested `check_output` reached the PARENT's stream (the client saw `retry`, then the subagent's text as the answer). `_run_graph_stream` ignores every event tagged `metadata.subagent_name`.
- **Bug:** the cached nested graph's `MemorySaver` kept every run's checkpoints for the process's life (201 threads after 200 runs). `run_subagent_impl` deletes its own thread in a `finally` (R02).
- **Gap:** a new subagent needs a process restart.

### 47. Example domains as a load-bearing proof
`app/domains/support/`, `ops/` and `sales/` run on the same unmodified graph as three products (a support copilot, an internal ops bot, a sales concierge), each following the `sql_store.py`/`tools.py` conventions plus `app/domains/shared/policy.py::ActionAllowlistPolicy`. A process builds a graph per domain name (96).
- **An unattended cron job can never approve itself.** `post_to_team_channel` is the first real `outward` tool and the gate has no bypass, so `scripts/jobs/ops_digest.py` and `followup_sweep.py` call the domain's `_impl` functions directly: a fixed pipeline that never enters the tool-calling loop.

### 48. Real-backend testing at every layer
`tests/containers.py` provides `ensure_*()` helpers that start ephemeral testcontainers, so `tests/integration/` and `tests/live/` run in CI while a no-Docker `pytest -q` stays fast. `promptfoo/` and `garak/` target the same small Ollama model.
- **xdist:** shared containers need a fixed cache dir; `AsyncPostgresSaver.setup()` runs once under a lock.
- **Small local models make poor judges and targets.** garak's `dan` probe had 100% attack success on `qwen2.5:1.5b`, so `app/core/moderation.py` is the real defence; a local-judge run was unreliable, so the judge is hosted (Gemini, Groq) with failover (`tests/deepeval/fallback.py`) and the target stays local. deepeval runs `flaky=True` in CI, so only a crash fails the job.
- **Bug:** a session-scoped stack leaked a saved memory into a later test, which made the 3B model propose `add_note` and pause for an approval nobody could give (~40 CI runs). An autouse fixture deletes each test's memories.
- **A gate must fail for a reason a change can cause.** Tests that need a 3B model to chain tool calls carry `@pytest.mark.advisory` and run in a non-blocking step; `-m "e2e and not advisory"` is the gate. Trade-off: a regression only in skills or subagents shows in that step's log.
- Tests wait for the page's "turn over" signal, not the text, and a failed browser test prints the page transcript (`tests/live/transcript.py`). `faulthandler_timeout = 300` names the line of a stalled test.

### 49. Per-domain requests streams
`requests_stream_key(domain)` (`agent:requests:{domain}`) gives each domain its own stream and consumer group; `get_domain` reads `X-Domain` (default `ecorp`). The domain is inferred from the stream a message landed on, never carried in the payload, so a resume or cancel lands where the turn did. An unknown `X-Domain` is a 422 rather than a publish nobody reads.

### 50. Web crawling (crawl4ai) and a sandbox (OpenSandbox over MCP)
crawl4ai (`app/adapters/outbound/crawl4ai/web_crawler.py`) renders JS-heavy pages; OpenSandbox, consumed over MCP through 28, runs code in isolation. Both go through the SSRF guard (`app/core/url_safety.py`) and every tool is `outward`, so the approval gate always fires.
- **Wrapped narrow:** a 3B model given OpenSandbox's ~19 tools hallucinated a `sandbox_id`. `app/domains/shared/sandbox_session.py` does lookup and creation in code and exposes four flat tools; `run_python_in_sandbox` takes the script as a plain argument because the model kept breaking shell quoting.
- **Check the structure before blaming the model:** a skill-based question never reached `skill_search` because those tools were last in the list; moving them first fixed every domain.
- **`check_output` gained two retry reasons:** `fabricated` (an invented tool result, ranked first) and `skipped_tool`.
- **Bug:** two simultaneous tool calls arrived glued into one `tool_calls` entry. `parallel_tool_calls=False` was a no-op; the fix is the LiteLLM chunk-index patch (`deploy/litellm/patches/sitecustomize.py`), which is load-bearing.
- **Bug:** a many-to-one rewrite of a Telegram id let two tenants share a sandbox and read each other's files (Principle I). Tenant and conversation id are now separate SHA-256 tags, lookups filter on both, and `tenant` is a required keyword on every sandbox helper.
- **Hardened (78):** the API key moved from the command line to the environment, the app pins the 5 of the bridge's 19 tools it uses by hash, results and arguments are bounded (`MCP_TOOL_OUTPUT_MAX_CHARS`, `SANDBOX_ARG_MAX_CHARS`), a bridge that is down alerts (`SandboxToolsUnavailable`), and one bridge process serves a worker's calls.
- **Not verified against a real service:** that the sandbox honours the metadata filter and its "no network egress" claim (spec 009, A2). The catalog is a shared resource; the approval gate is the boundary.

### 51-66. (moved)
These are not patterns of the graph. Their numbers are kept so references ("pattern 58") resolve.

| Patterns | Now in |
|---|---|
| 26, 51, 57, 58, 62, 63, 64: the usage meter, the spend limits, retiring the per-turn ledger | [`COST_GOVERNANCE.md`](COST_GOVERNANCE.md) |
| 59, 60: a settings error must not print its secret; re-applying a migration in a test | [`ENGINEERING_PATTERNS.md`](ENGINEERING_PATTERNS.md) |
| 52-56, 61, 65, 66: the credit wallet, webhooks, export, reconciliation, provider adapters | [`packages/billing/docs/PATTERNS.md`](../packages/billing/docs/PATTERNS.md) |

### 67. Embedding roles and identity: a question is not a passage, and the same size is not the same space
Retrieval embeds a question and a stored passage, and one function served both. Changing what you embed changes the space, and Qdrant cannot tell: two 768-d models accept each other's queries and return confident nonsense.
- **Two roles:** `embed_query` / `embed_document(s)` replace `embed_text` (an `ImportError`, not a deprecation); an AST test fails if anything under `app/` or `scripts/` calls the raw client around them. `EMBED_QUERY_PREFIX` / `EMBED_DOCUMENT_PREFIX` default to empty; whether a prefix helps is measured (`make model-eval`).
- **Identity in the collection's name:** (model the `embed` alias resolves to, vector size, both prefixes, `EMBED_MODEL_REVISION`) hashed into the physical name (`docs-1a2b3c4d`). Every read and write goes through the alias `docs-live` and compares the suffix (`qdrant_store.check_identity`). A mismatch is **refused**: a read degrades like a retrieval outage, a write fails, `agent_embedding_identity_total{outcome="mismatch"}` counts it, `GET /health/ready` names the collection and `EmbeddingIdentityMismatch` fires.
- **The swap:** `make reembed COLLECTION_NAME=docs` builds the new collection beside the live one from each point's stored text, verifies it, then repoints the alias in ONE request; the old collection is never deleted. The answer cache's index carries the hash too.
- **`/v1/embeddings` on the ML service** speaks the OpenAI shape, so pointing the `embed` alias at an ONNX model there is configuration only. The server never applies a prefix.
- **Caught on the way:** an autouse guard now refuses a real Qdrant connection in the default suite (Constitution VII), and `make reembed` takes `COLLECTION_NAME` because `COLLECTION` is also a Settings variable.
- **Gaps:** a collection from before identities (a physical `docs` with no alias) is *adopted* and trusted, not verified, until the first `make reembed`; the identity resolver is LiteLLM's `/model/info`, so where nothing answers the alias name stands in (`unverified`, blind to the same name re-pulled to different weights: that is what `EMBED_MODEL_REVISION` is for); writes during a re-embed are detected, not lost (exit 4); the default is still nomic through Ollama with prefixes off.

### 68. A model-routing seam that can only advise, from two signals and in three tiers
An operator can attach up to two more OpenAI-compatible models behind gateway aliases (`chat-medium`, `chat-strong`). The router picks a tier per turn from rules over the message (`app/core/routing_policy.py`) and a label from the ML service's `route-topics` label set (specs/015): `min(ceiling, max(rules, label))`, lowered to the highest tier that is configured and priced. `ROUTER_MODE=shadow` shows what it would have spent before it spends anything.
- **The risk is a second authority.** If the router could affect which tools a turn may call, whether a write needs approval, whose data is read or whether a write repeats, it would sit beside the three non-negotiable rules. So `route_model` writes `State.model_route` and nothing else; one test pins the single key, another runs the same mutating `add_note` turn under all three tiers and asserts the same approval pause, tenant-scoped payload and `idempotent()` key.
- **Where it sits:** after moderation, cache and compaction, in parallel with retrieval; never for a refused input, a cache hit, a resumed turn or a subagent. `validate_input` resets the route each turn.
- **What it sends:** the first 2,000 characters of the current message and the label set to `POST /v1/decide`; never tenant, principal, history or retrieved text. The answer is validated, not trusted, and a missing label never cancels the rules (`ModelRouterLabelUnavailable` fires when over half of routed turns for 15 minutes had to).
- **Three modes, one code path:** `off`, `shadow` (decides, counts in `agent_model_route_tier_total`, runs local) and `on`. `routing_policy.decide` is pure, so the evaluation harness runs the same code.
- **A failing routed model is handled in `agent()`, not by the gateway:** a connection error, timeout or 5xx repeats that call on the local alias (`ModelRoutedTierFailing`). A gateway `fallbacks` entry would be wrong because the usage event would price a model that did not serve (a test fails if one is added). LiteLLM in front of an unreachable upstream answers HTTP **500**, not 502/503/504. A routed alias with no price under `UNPRICED_MODEL_POLICY=block` steps down a tier, so the router can never be why a turn is refused.
- **Dev stand-ins are local;** in production both aliases default to `chat` until `LLM_MEDIUM_*` / `LLM_STRONG_*` are set. A mode with no alias, or two tiers sharing one, refuses to start.
- **Not built:** per-tenant control (keep both aliases on acceptable models or set `ROUTER_MAX_TIER=local`).
- **Gaps:** a routed endpoint that accepts the connection and never answers is not seen by the fallback; the label table and rules are policy, not a measurement (nothing shows routing improves an answer; the label set is measured, `make model-eval KIND=classifier ... DATASET=route-topics`); only the current message is read, so "yes, do it" falls to the rules.

### 70. A check judges what was said, not where a marker or a pleasantry landed
A correct, cited answer was rejected three times and the person told "I described what I'd do instead of actually doing it". Two stacked heuristics, each retry worse.
- **Bug:** a marker written after the full stop (`... are free. [1]`) was judged against the NEXT sentence and called `misattributed`. `sentence_spans` / `split_sentences` keep a marker that follows the full stop on the same line with its sentence.
- **Bug:** a closing question ("Is there anything else I can help with?") rejected a complete answer, or was only sometimes seen, and the rewrite could not work because the model writes it anyway. `output_guardrails.strip_closing_offer` now cuts the offer from the END of an answer, only when what is left is itself an answer (at least four distinctive words, no unclosed code fence); leak and fabrication are still judged on everything the model wrote. Metric: `agent_closing_offer_stripped_total`.
- **Gap:** the cut is English and a closed list of cues, and four distinctive words is a threshold, not a measurement.

### 71. A refusal is final: not retried, not an error, and not left in the thread
LiteLLM's "Denied Financial Advice" guardrail refused a question with an HTTP 400, and three things turned one policy decision into a dead session.
- **Bug:** every 4xx was retried (LangGraph 0.2.76 excludes only `httpx.HTTPStatusError` 4xx, not the OpenAI SDK's classes). `gateway.is_permanent_client_error` (a 4xx other than 408, 409, 425, 429) is now refused a retry.
- **Bug:** the refusal reached the person as an anonymous `internal` error. `gateway.content_block` recognises it by LiteLLM's structured fields, the `agent` node makes it the turn's answer ("I can't help with that request."), marked with `GATEWAY_REFUSAL_KEY`, and `graph_edge_route_after_agent` sends it to `no_answer`: no retry, no follow-ups, no cache write. Any other 400 still raises: those are bugs.
- **Bug:** the refused message stayed in the thread and the gateway scans the whole list, so every later turn was refused too. A `pre_call` refusal now replaces this turn's message with a placeholder (same id).
- **Gap:** a thread whose OLDER messages already match a newly added guardrail stays refused and needs a new session.

### 73. The chat model is chosen by what it does with this app's tool calls, and a sampling option can break them
"Remember this" never ran `remember`. Ollama silently discards a tool call whose JSON it cannot parse, and the dropped replies were missing a closing quote after a key.
- **Two causes:** the gateway's `repeat_penalty: 1.3` (it penalises the `":` and quote tokens a call repeats) and `qwen2.5:3b` being unreliable even without it. `scripts/checks/toolcall_probe.py` (22 labelled prompts, the Ecorp tools) scored tool-needing prompts answered, out of 19:

  | repeat_penalty | `qwen2.5:3b` | `qwen2.5:7b` | `qwen2.5:1.5b` |
  |---|---|---|---|
  | 1.3 (old) | 8 | 11 | 5 |
  | 1.1 (default) | 13 | **19** | 12 |
  | 1.0 | 15 | **19** | 12 |

- **Decision:** `chat` and `chat-backup` are `qwen2.5:7b` at `repeat_penalty: 1.1`. The 3b loops at any setting; `llama3.1:8b` called a tool on all three prompts that need none. `chat-strong` is `qwen2.5:14b` (`make pull-strong-model`), so escalating changes something.
- **The check is a command:** `make probe-tools` runs both halves (tool calls, and open-ended prompts that must not loop) and exits 1 on any dropped call, any loop, or under 90%. Run it after ANY change to a chat alias's model or `repeat_penalty`.
- **Gaps:** 22 prompts on one machine is a screen, not a ranking; CI's small models (`qwen2.5:3b`, `1.5b`) score 13/19 and 12/19, a plausible cause of the flaky live tests, not yet tested; the `vision` alias keeps `repeat_penalty: 1.3`; `make eval` fails two checks on old and new models alike (a retrieval case the pre-fetch makes unnecessary, and a general-knowledge answer citing an invented `[1]`).

### 74. Strict tool schemas are a form the model is sent, not the contract the app enforces, and they are off until a provider enforces them
`TOOL_CALLS_STRICT` makes `make_llm` send every tool in OpenAI strict form through `strict_tool_schema` (`app/core/tool_schema.py`). It exists, and is **off**.
- **LangChain's `strict=True` does not make a tool strict-valid:** it closes objects and sets the flag, nothing else. Six of the 32 tools across the four domains had optional arguments missing from `required`. `strict_tool_schema` lists every property as required (optional ones nullable), closes every object and turns defaults and length bounds into description text; `tests/core/test_tool_schema.py` checks every tool of every domain.
- **A missing nullable argument is filled with `None` before validation.** `qwen2.5:7b` does not follow "required" and sends only the arguments it needs; refusing that failed 7 of 19 probe prompts, and no prompt wording fixed it reliably. Each function keeps `= None` in its signature.
- **Why off:** LiteLLM's `ollama_chat` hands `tools` to Ollama untouched, and the strict form costs a little (7b 19 to 18, 3b 14 to 12, 1.5b 9 to 8). Turn it on together with the change that points `chat` at a provider that enforces it, and run `make probe-tools` before and after.
- **Not done:** `extra="forbid"` on the argument models (it would fail an extra argument a small model invents, harmless because identity comes from `ctx`).
- **Gaps:** never run against a strict provider; strict output is not guaranteed with parallel tool calls, which this graph supports; the probe covers Ecorp's tools only.

### 78. A published MCP server acts for whoever launched it, and a remote server is untrusted input
MCP runs both ways (`app/adapters/inbound/mcp/` publishes, `app/adapters/outbound/mcp/` consumes), and one rule runs through both: the other side of the pipe is not where a trust decision gets made.
- **Bug:** the model chose the tenant. `query_employees` took `tenant` and `principal` as arguments because MCP has no `RunnableConfig`, so a page a connected assistant had read could name another tenant. Identity is now bound at process start (`MCP_TENANT`, `MCP_PRINCIPAL`, `identity.py`); no tool exposes either name, a call that sends them is ignored, and a server started without one exits before serving. Tests spawn the real server and check the wire schema and that stdout stays empty.
- **Read-only tools say so** (`readOnlyHint`, `idempotentHint`), and every call logs `mcp_tool_call` as JSON on stderr (never an argument or result).
- **A remote catalog is pinned.** A tool description is text the model obeys, which is where tool-poisoning lives. `pinned` maps each expected tool to a hash of its name, description and input schema (`tool_fingerprint`): an unlisted tool is dropped, a changed one is dropped and counted (`McpToolCatalogChanged`), and a test importing the bridge fails the same drift in CI.
- **What comes back is rendered, scrubbed, then bounded** (`app/core/output_limits.py`). Scrub before cutting, or a key straddling the cut splits into halves no pattern recognises; the cap keeps head and tail.
- **One connection per server and loop, and never a second send of a call that may have run** (`connection.py`). mcp 2.x cannot tell a call that never left from one the server ran and died on, so the connection pings first: a ping that finds it closed proves the call never left, so it is sent once on a new connection. A call in flight is NOT replayed (Principle IV); a test has the server run a side effect and die, and asserts it ran once. A lost in-flight call is a failed tool call, and `idempotent()` cannot key a call the model reissues, so the `outward` approval gate is what stands there.
- **The app is on mcp 2.x because `opensandbox-mcp` 1.1 is** (it needs `mcp[cli]>=2,<3`). The SDK's `Client` class is not used: the protocol revision it speaks removes `ping`, which the no-replay guard rests on (`test_connection.py` fails if the connection lands on one). SDK, bridge, server and sidecar images move together at one version, with digests.
- **Gap:** nothing authenticates whoever starts a published server; over stdio that is the machine's owner, and the networked case is the gateway (82).
- **Use when:** an MCP server is started by someone other than the person it acts for, or a tool catalog comes from code you did not write.

### 79. A call is built for the server's prompt cache: nothing that changes per turn goes in a system message, and a call that needs no tools sends none
Two turns took 26.8 s and 40.1 s for answers that took seconds to write.
- **Measured: Ollama hoists every system message** into the template's one system slot. With only the last user message changed, the prompt cache matched 2461 of 2467 tokens; with one mid-conversation system message it matched 8.
- **Bug:** the retrieved context and every reminder were system messages, so the start of the prompt differed on every turn (5,448 of 6,360 prompt tokens re-read, 17 of 18.5 s). `messages.with_turn_notes` now puts the notes on a COPY of the turn's question; the stored thread is not edited and the domain prompt is the only system message. Replayed on the same 62-message history: 26.2 s cold, then **1.1 s** for the next question.
- **Bug:** a one-sentence follow-up prompt carried 15 tool schemas (2,869 tokens, a cold prefix). `utils.without_tools` takes the bindings off `suggest_followups` and `compact_history`.
- **Bug:** a link was described, not opened. A prompt sentence did nothing on the 7b; `links.LINK_REMINDER`, appended last and only when the question has a link and the domain has no address-reaching tool, did. The citation reminder now ends "If none of that content helps answer the question, ignore it and cite nothing."
- **Gaps:** measured on one server and model (elsewhere the change is neutral, not a cache fix); retrieved text now rides in a user message, a less privileged role than a system message, and `garak` and `promptfoo-redteam` were not run: run them before relying on the move. An unrelated chunk is still sent (`MIN_RERANK_SCORE = -8.0` is a model decision, 69); a dropped tool call on a long context is unexplained (`agent_unparsed_completion_total` stays); the first request after a deploy or an idle gap is cold.

### 80. A stopped turn is taken out of the thread, because the model reads the thread and not the click
A turn was stopped with `POST /chat/cancel`, then a new question was sent. The model saw both messages, answered the older one, and paused to run the script the person had just stopped.
- **Fix:** `runtime/cancelled_turn.discard_cancelled_turn`, on `TurnCancelledError` only, removes the turn's messages (`RemoveMessage`) with `aupdate_state(as_node=START)`. A timeout, error or closed connection keeps the question so "try again" has something to retry. If a TOOL already ran, the turn stays and is closed with an assistant line, because forgetting a filed ticket invites filing it again under a new call id, which the exactly-once guard cannot catch.
- **Read in the installed LangGraph 0.2.76, not assumed:** `update_state` without `as_node` raises "Ambiguous update" after a run that stopped part-way, and the next turn's input discards the tasks left pending. Re-verify with the LangGraph upgrade. Metric: `agent_cancelled_turn_discarded_total{outcome}`; it never raises.
- **Gaps:** a cancel while paused at approval (`cancel_run`) is unchanged; text already streamed to the page stays there until reload.

### 81. A tool that failed is not a promise unkept, and a database error is not for the person to read
- **Bug:** the deferral check could not tell a promise from an account of a failure. With the sandbox down the model counted a log by hand and said so; the check matched "let's ... manually", and after two retries the correct count was replaced by a fallback that also hid the outage. `_deferred` no longer fires when a `ToolMessage` with `status="error"` is in the current turn.
- **Bug:** a `psycopg.Error` (a missing table) went to the person verbatim. The model now gets the class name only, is told not to retry and to say the lookup is unavailable; the full message stays on the span.
- **Bug:** a database that predates a schema script said "ready" (`postgres-init/` runs on a fresh volume only). `GET /health/ready` now lists missing tables and the scripts to apply, in order (`health.APPDATA_TABLES`); it never changes the status or applies anything.
- **Gaps:** any failed tool excuses the whole turn's narration; only the database driver's errors are redacted (a sandbox network error still says "try a different approach"); the schema check covers tables, not columns.

### 82. The same two MCP servers over HTTP take the identity from a verified token on every request, and neither the audience nor the scope is the boundary alone
`app/adapters/inbound/mcp/gateway.py` (specs/017) serves `query_employees` and the ops tools over Streamable HTTP. It is 78's rule with a new source: the identity is the verified access token's `owner` (tenant) and `sub` (principal), never an argument and never the launcher's environment (a test greps the gateway for `mcp_tenant`/`launcher_ctx`).
- **A resource server, and nothing else.** It issues no token. It checks signature against the issuer's JWKS with an algorithm from an allow-list (never the token's own header, so `alg: none` and HS256-with-the-public-key are refused), `iss`, `exp`, `sub`, and `aud` containing THIS endpoint's URL. The verifier never raises, since an exception inside the SDK's middleware is a 500.
- **Each server is its own resource** (`/ecorp/mcp` with scope `ecorp:read`, `/ops/mcp` with `ops:read`), and both checks are needed: Casdoor 4.24.0 mints a token with any `resource` and any scope the application defines, without relating them. Ops data belongs to the deployment, so `TenantGate` admits only `MCP_GATEWAY_OPS_TENANTS` (empty means nobody).
- **Keys:** fetched once, refreshed on an unknown `kid` at most once per interval (made-up key ids cannot hammer the issuer), never trusted past the TTL; when keys cannot be fetched a token is refused, `jwks_unavailable` counts, `McpGatewayCannotCheckTokens` fires and `/readyz` goes 503.
- **Stateless, so scaling is a flag:** any replica answers any request; Caddy finds them and `lb_try_duration` re-dials a failed connect (a replica killed under load failed 0 of 615 calls).
- **Not built:** a real Claude Code login (the smoke test performs the same sequence), token revocation (a token is good for its hour), a stale-key grace. **The sandbox and any write tool are not behind it**: Principle II puts every side effect behind an approval gate that lives in the agent's graph, so that is a decision (specs/017 T022), not a gap.
- **Use when:** an MCP server must be reached by someone who did not start it, or by more than one replica.

### 84. The front door checks the token once and hands the API a short-lived signed assertion; a path the proxy calls public never reaches the check behind it
`services/authlimit` (specs/018) is a Go service that Caddy asks about every request (`forward_auth`) before the chat API or an MCP route. It verifies the Casdoor token **offline**, charges per-tenant and per-person token buckets, refuses revoked people and returns a **30-second Ed25519-signed identity** (`X-Internal-Identity`) that Caddy copies onto the request after removing the caller's `Authorization`. Asymmetric on purpose: an HMAC secret shared with consumers would let any consumer mint an identity for any tenant. Key rotation needs no downtime. The API's half is 86.
- **Measured on the pinned Caddy:** `copy_headers` deletes a client-sent copy before setting, so a forged identity header never survives; the upstream still receives `Authorization` unless `request_header -Authorization` follows the check.
- **Bug (found by the real-stack smoke, not a unit test):** a request the proxy declares public never reaches the check. `/static/a%5c..%5cchat/resume` matched Caddy's `/static/*` and went to the upstream with no token. **Rule: whichever component declares a route public must be the strictest reader of the URL.** Only plain URLs (no `%`, backslash, `..` or `//`) are public now; the smoke runs eight path tricks.
- **Bug:** a dead replica cost the dial timeout on every request until the next health check. One failed connect now skips the replica for 20 s and the dial timeout is 1 s.
- **Limit-only mode on the MCP routes:** the gateway must verify the token itself, so the service verifies only enough to know who is calling and **never refuses a token the gateway accepts** (it would pass through un-metered). The 37 shared vectors (`testdata/auth-vectors/`) are run by both verifiers.
- **Two failure policies, one place:** a security check fails closed (no usable keys, a revocation list too old: 503), a protection fails open (the limiter's store down: served, counted, alerted). `services/authlimit/internal/server/server.go` says which is which.
- **Not built:** Cloudflare, a production identity provider, mutual TLS between Caddy and the service, a managed key store.
- **Use when:** one place should prove who is calling and meter them for several services. (A request with no token to verify is 97.)

### 85. Revocation is a local copy refreshed on a short interval, so a store outage neither stalls a request nor un-revokes anyone
Access tokens last seven days and are verified offline, so `authlimit revoke` bounds a stolen one (specs/018). It records "tokens issued to this person at or before T are void", not token ids (a token with no issue time counts as issued at 0, the safe direction).
- **Each replica copies the whole list into memory every second; a request is a map lookup.** A Redis call per request would make the store's health part of every request's latency and force ONE failure policy on two things that need opposite ones: the limiter (fail open) and revocation (fail closed).
- **A replica that cannot refresh keeps enforcing its copy;** only past 30 s does the policy apply (refuse by default). It is checked before the limiter, so a revoked token cannot drain its tenant's bucket, and it applies to the MCP routes too. Measured: first refusal 0.3-0.5 s after `authlimit revoke`, every replica within 3.2 s.
- **Not built:** tenant-wide revocation, introspection against the authorization server.

### 86. The API builds its caller from the identity the front door signed and nothing a client sends; keys it cannot confirm are a 503, never a guess
`get_ctx` (`app/adapters/inbound/api/deps.py`) once took "who is calling" from two headers any client can set, so a person logged in as tenant A could send `X-Tenant-Id: B` and be served B's conversations, usage and uploads.
- **Two modes, chosen by name:** `AUTH_MODE=identity` (the default) builds the ctx from the verified identity's `tenant` and `sub` alone and does not read the headers; `headers` is for the hermetic tests and a local API alone (`.env.example` selects it, production's example selects `identity`). It announces itself: a startup warning, the gauge `agent_api_auth_mode{mode}` (re-set every minute, because the collector forgets a series after 5) and the alert `ApiHeaderAuthMode`.
- **The check never raises** (`app/platform/internal_identity.py`): every failure is a `Refusal` with a reason from a closed list, counted in `agent_api_identity_total{outcome}`. EdDSA comes from an allow-list; `typ`, `iss`, `aud`, `exp`, `nbf` are checked. A refusal never says why: every bad identity, and none at all, is the same 401 in the door's envelope.
- **Keys the API cannot confirm are a 503 `auth_unavailable`, not a 401** (a 401 would send a logged-in person to sign in during an outage). Keys are fetched with a 5 s deadline, refetched on an unknown `kid` at most once per 10 s, and kept through a failed refresh for 600 s; past that, **closed**. The API refuses to start in identity mode without a usable `AUTHLIMIT_KEYS_URL`.
- **The web page signs in itself** (`static/auth.js`, no library): authorization code with PKCE (S256), the code removed from the address bar before exchange, the token in `sessionStorage` only and sent only to this origin, `resource` on both the authorize request and the exchange (without it Casdoor refused the exchange; only a real browser run showed it).
- **Verified three ways:** a smoke against the real API (`make authlimit-smoke ARGS="--upstream api"`: a forged header and even a VALIDLY SIGNED forged identity change nothing), the page in Chromium (`make authlimit-web-check`), and the page's script against a DOM stub in the default suite.
- **Not built:** a refresh token in the page, a server-side session cookie, a Content-Security-Policy. `GET /ingest/stream/{job_id}` asks for no caller: any signed-in person who knows a job id can follow its progress.
- **Use when:** the identity a service acts for must come from something the caller could not have written.

### 87. A web search is a read that sends text out and spends money, so it waits for a human; what comes back is data and must not look like a citation
`web_search` (serper.dev: `app/adapters/outbound/web/serper.py`, tool in `app/domains/shared/web_search.py`) is the missing first half of the page-reading tools in support, ops and sales; Ecorp, which cannot open links, does not get it.
- **`outward`, though it changes nothing:** the query is model-written text sent to a third party and every call spends a credit, in a context that is untrusted on nearly every turn (12). It is wrapped in `idempotent()`, so a replay returns the stored result instead of spending a second credit.
- **A web result is never numbered `[1] [2]`:** those are the knowledge base's (`graph/citations.py`), so a result is a title, an address and a snippet, framed with `frame_untrusted`, and a bracketed number in a snippet becomes `(n)`.
- **Only a connection that never opened is retried** (`ConnectError`, `ConnectTimeout`); a read timeout may have been billed ("retry only what provably never landed", Principle IV). A blank query is refused (it was billed). Provider error text goes to the log, never to the model.
- **Always declared, even with no key:** the tool list must not change between deploys, or a thread paused on an approval for it could not resume. With no key it answers "web search is not set up" as a normal reply.
- **Metric and alert:** `agent_web_search_total{outcome}`, `WebSearchProviderRefusing`. `make web-search-check` makes the one real call (one credit); the hermetic suite sees an empty key (`no_real_web_search`).
- **Gaps:** an out-of-credits account and a 429 were never observed (mapped by status class); only `organic` results are read; the spend is not metered (the approval gate and `WEB_SEARCH_MAX_RESULTS` bound it; no per-tenant quota, no result cache); nothing scans the query for customer data beyond the prompt and the human reading it at approval.
- **Use when:** a tool sends model-written text to a third party, or its results are text a stranger could have written.

### 89. Every front door runs its turns on the queue, through the front door, and "nobody can answer an approval" is a property of the request
The CLI and Telegram once ran the graph in their own process (42, 43), a second execution path that the worker's lock, crash reclaim and dead-letter archive never reached, and then published to the queue with an identity they built themselves, so the door's limits and revocation (84) never applied. Both are now clients of `POST /chat/stream/queued`, `/chat/resume` and `/chat/cancel` through the door (`channels/door.py`), each as a real person (91). Nothing but the API writes to the queue; `test_channels_use_the_door.py` fails the build if a channel can reach the queue or the graph.
- **Unattended is a field of the request:** it rides on the job, so it survives a reclaim, and the worker declines a pause instead of forwarding it. The API accepts it from any caller because it can only make a turn stricter. Without it a crashed Telegram turn would have been continued as an ordinary one and left the conversation paused.
- **A redelivered update would be a second turn.** A turn on a worker outlives the channel, so replaying it is a second model call. The channel sends the update number as `replay_key` (`telegram:<domain>:update:<id>`), claimed for `RESULTS_STREAM_TTL_SECONDS`, scoped to the conversation, and given back if the publish fails.
- **A conversation id that names its owner:** a `telegram:` id is claimable only by the caller it names (`telegram:<person>:<chat>`), so each person in a group chat has their own conversation.
- **The CLI cancels on the way out** (Ctrl-C mid-turn calls `/chat/cancel`). **Failure is words, and there is no other route:** every refusal becomes one `error` event (counted in `channel_door_refusal_total`), and when the door is unreachable nothing falls back to the queue.
- **Rollout order: workers first.**
- **Gaps:** the dedup claim lasts 300 s, so a slower restart replays the turn; `scripts/tools/ops_investigate.py` still builds its own one-shot graph.
- **Use when:** more than one front door shares a conversation store and a side-effect policy, and the guarantees live in one executor behind one door.

### 91. A client that signs in for people must not hold a credential that can create them
Moving Telegram behind the door (89) meant the bot had to *be* each Telegram user at the authorization server. An application credential that creates a person on a first message was measured (Casdoor 4.24.0, specs/020 research R1) and is wrong: an application's client id and secret administer its whole organization (reset any password, delete a person). A leak of the bot's environment would be a takeover of every person in the tenant.
- **Derive, don't store:** a person's password is `HMAC(TELEGRAM_PERSON_SECRET, tenant, id)`, so a restart gives the same people and the bot has nothing per person to lose.
- **Who creates a person:** an operator by default (`make telegram-person ACTION=add TELEGRAM_ID=<id>`); `TELEGRAM_AUTO_PROVISION` is an opt-in that creates one on a first message under a lock per Telegram id.
- **Disabled is final.** A forbidden person is never re-created; a wrong password is never retried (five lock a person). A token already issued stays valid until it expires or `make authlimit-revoke` voids it.
- **The sign-in is the authorization-code flow, headless.** Measured: the password grant ignores `resource`, so its token's audience is the client id and the door refuses it.
- **Gap:** a Telegram person's password also signs in at the web page and the MCP sign-in, so a leaked `TELEGRAM_PERSON_SECRET` can be used as those people anywhere in the tenant; with `TELEGRAM_AUTO_PROVISION` on, the bot's environment holds a tenant-wide credential, so use a tenant of its own.

### 92. A button in a chat approves only when the person it was shown to presses it, once, in time
Approve / Reject buttons in a Telegram chat have five ways to become a hole (Principle II), each a test (`channels/telegram/test_bot.py`):
- **A group chat shows the buttons to everyone:** the approval record names the Telegram user it was shown for, and a tap from anyone else is refused and leaves the approval standing. The API repeats the check for the signed-in person.
- **A double tap or redelivered callback must not resume twice:** the first tap claims the approval (`SET NX`). A decision the door itself refused gives the claim back and restores the buttons; one the agent refused (`checkpoint_lost`) is reported, not offered again.
- **A late tap must not cancel anything:** `/chat/cancel` would stop a turn that is running by then, so a tap after expiry only says so.
- **An approval nobody answers strands the conversation:** the record expires (`TELEGRAM_APPROVAL_TTL_SECONDS`), and the next message cancels the stale pause, waits for that job, and resends under a new replay key. While an approval is live, a new message gets a pointer to the buttons and never reaches the agent.
- **Buttons that cannot be shown cannot be answered:** if the keyboard message does not send, the pause is cancelled at once.
- `callback_data` is a random token, never the subject. `TELEGRAM_APPROVALS=false` is the old behaviour (decline unattended).
- **Gaps:** expiry is lazy (the buttons stay on screen); `/cancel` cannot stop a RUNNING unattended turn; not run against the real Telegram.

### 93. Acknowledge an update when it is durable, not when it is answered
One sequential loop meant one slow turn delayed every chat, `/cancel` could not exist, and a webhook would have been worse (Telegram redelivers a request not answered 2xx quickly).
- **Both transports only push:** long-polling and the webhook put the update on a Redis list and acknowledge once it is there; `telegram/inbox.py` takes updates into a `processing` list and removes them when handled.
- **Parallel across chats, in order within one,** bounded by `TELEGRAM_MAX_CONCURRENT_CHATS`; a chat holds one slot however many messages it has queued; `/cancel` is routed around the chat's queue.
- **Crash-safe, and not forever:** in-flight updates return to the FRONT with an attempt counted; past `MAX_ATTEMPTS` (3) one is dropped and counted (`channel_telegram_update_total{outcome="dropped"}` alerts), so a poison update cannot loop the bot. Handling is at-least-once, which the handlers tolerate (89, 92).
- **Webhook:** the `secret_token` must arrive in `X-Telegram-Bot-Api-Secret-Token`, compared in constant time (403 otherwise, nothing queued); Redis down is a 503 so Telegram retries. The listener binds loopback and the proxy must NOT require the door's token on `/telegram/*`.
- **Gaps:** one bot process per domain; the in-memory backlog is capped; the webhook was never called by the real Telegram.

### 94. Linking an account takes the person's own login, and the bot keeps only what it can afford to lose
`telegram-<id>` people (91) prove nothing about WHO is behind a Telegram id. `/login` binds a Telegram id to the account the person signs in as, in their own browser at the authorization server; the bot never sees a password.
- **State is single use, short lived and bound to the Telegram id that asked.** A confirmation page exists for one attack: someone sends THEIR `/login` link to a victim, and without a question the attacker's Telegram is bound to the victim's account. Binding happens only on that button.
- **Only a refresh token is kept, sealed with Fernet (`TELEGRAM_LINK_KEY`).** Each turn refreshes it into an access token the door verifies, so authority is what the authorization server says now: a disabled person is cut off at the next refresh. The tenant comes from the token (`owner`); another tenant's account is refused (Principle I). Refreshes of one person are serialized, because a rotating server would see the old token twice and revoke the link.
- **`/login` is private-chat only;** the pages set `no-store`, no referrer, no framing and a locked-down CSP, since the address carries a one-time code.
- **Gaps:** whoever controls a linked Telegram account acts as the person (a SIM swap); `/login` is not rate limited by the bot; losing `TELEGRAM_LINK_KEY` unlinks everyone, and leaking it with Redis exposes the refresh tokens; refresh-token rotation was not measured against real Casdoor.

### 96. One worker serves many domains; a job's domain is the stream it came from
`AGENT_WORKER_DOMAINS` (names, or `all`) lets one process read every named domain's requests stream (49) in one `XREADGROUP` and run each job on that domain's graph, instead of one container per domain with its own idle budget.
- **The domain is bound for the task, not passed down:** `bind_domain` (`runtime/core.py`, a `ContextVar`) wraps the handler, and `init_graph_async()` and `upsert_session` read it.
- **One checkpointer, a graph per domain** (`PgCheckpointing.open()` makes a new pool each call, so opening per graph would orphan all but one). The ack, dead letter and retry name the job's own stream, and the reclaim loop sweeps every served domain's stream.
- **A typo or a bad manifest stops the process at boot.**
- **Gap:** the concurrency budget is shared, so a flood on one domain can hold every slot (split the pools then; [WORKER_CONCURRENCY.md](WORKER_CONCURRENCY.md)); serving several domains against real Redis and a real model is untried.
- **Use when:** domains differ in tools and prompt, not in how they are served.

### 97. A request with no token is charged to the network address Caddy resolved, and a protection that cannot read the address limits everyone, not no one
Sign-in and webhooks carry no token and cannot, so the door (84) never saw them, and the only limit was the app's per-address webhook limiter, which behind a proxy keys on the proxy: a global ceiling.
- **A second question:** `/v1/check` verifies a token and charges a person; `/v1/limit/signin` and `/v1/limit/webhook` verify nothing and charge one bucket per address (`address_limits` in `services/authlimit/routes.json`). The sign-in host charges only `/api/login*`, not the page and assets (an office behind one address would empty any bucket sized to stop guessing).
- **The address is Caddy's and a client cannot set it:** `header_up X-Client-Ip {client_ip}` SETS the header, so a forged value never survives (measured against the real Caddyfile).
- **An address it cannot read is one shared bucket, not no bucket:** a Caddy that stopped sending the header would otherwise switch the protection off silently. An IPv6 client is its /64.
- **Same script, one bucket named twice,** so there is one copy of the refill arithmetic. A path trick (`/api//login`, `/api/./login`) can cost a token, never skip the limit.
- **Failure policies:** a store outage serves the request; authlimit unreachable is a 502, so sign-in is down with the door.
- **Disclosed:** it bounds the *rate* one address can try; it does not stop someone locking a person out (Casdoor locks after five wrong passwords), and addresses behind one NAT share a bucket. The Telegram bot is one address for all its people, so `signin` (60 burst, 120 a minute) is its ceiling.
- **Use when:** a request cannot carry the identity the rest of the door charges, and an unbounded rate on it is a risk.

### 98. Production runs the same front door as development, from one routing file; the entries only say how a site is addressed and what sits behind it
Production ran `api` behind a Caddy that did not authenticate, and could not have served a chat request in identity mode. Copying the dev Caddyfile would have left two copies of the part that decides who gets in.
- **One routing file, two thin entries:** `deploy/caddy/gateway.caddy` holds everything that decides a request; `Caddyfile.gateway` (dev) and `Caddyfile` (prod) say how each site is addressed and certified and define the `api_proxy` and `telegram_proxy` snippets it uses.
- **Same services, same images, same settings; places and passwords are required:** `casdoor` (pinned by digest), `authlimit` x2, `mcp-gateway` and the opt-in `telegram-bot` are in `docker-compose.prod.yml`. `API_DOMAIN`, `AUTH_DOMAIN`, `MCP_DOMAIN` and the Casdoor passwords have no default, and the issuer, audience and keys URL are derived from them in one place.
- **Found by running it:** a `${VAR:?}` on the opt-in bot's token would have broken `pull` and `up` for everyone who does not run it (ENGINEERING_PATTERNS.md 83); a bare `tls` is a Caddy parse error; a Caddy that waits for authlimit to be healthy can never finish the first deploy (the signing keys cannot exist before the image does); the deploy workflow's last step curled a port `api` never publishes and now checks `/health/ready` through the door.
- **Gaps:** the admin UI shares the sign-in host and is protected by the admin password and the per-address limit only; the observability droplet's Prometheus is not reachable from the MCP gateway by default; nothing was run on a real droplet or against Let's Encrypt.
- **Use when:** more than one deployment shape fronts the same service and the part that decides who gets in must not be able to differ.

### 99. Three timeouts that must stay in order are a validator, not a comment
The queue is correct only while three numbers stay in order: how long a turn may run (`REQUEST_TIMEOUT_SECONDS`), how long a conversation's lock lives, and how long a claimed job may sit before it is presumed abandoned (`AGENT_WORKER_RECLAIM_IDLE_SECONDS`).
- **Bug:** raising the turn's limit made a healthy turn run twice (reclaimed at minute four). `Settings` now refuses a reclaim idle that does not exceed `REQUEST_TIMEOUT_SECONDS x (1 + UNATTENDED_MAX_DECLINE_ROUNDS)` and the lock's lifetime, each by 30 s, and names the variables, never the values (ENGINEERING_PATTERNS.md 59). `REQUESTS_STREAM_MAX_AGE_SECONDS` (43) must exceed both reclaim idles, because a trim drops pending entries too.
- **Bug:** the conversation lock was never renewed (120 s against an unattended turn of up to 240 s), so a second job could take it mid-turn: the fork the lock exists to prevent. The worker now renews it every third of its 60 s lifetime with a compare-and-extend script (`_keep_lock_alive`); a lock found lost is counted (`AgentThreadLockLost`) and logged.
- **Bug:** a worker claimed jobs it could not start, and a claimed entry's idle clock runs while it waits, so under saturation "idle" measured the queue. A worker now takes its free slots first (`claim_free_slots`) and asks for that many.
- **Gap:** `COUNT` is per stream, so a worker serving several domains with fewer free slots than streams can still claim an entry it must wait to start; a worker whose loop is blocked past the lock's 60 s still loses its lock.
- **Use when:** correctness depends on one timeout outlasting another. Write the relation as a check that fails at start-up, and make the test name the variables.

## Naming

One rule, so a function's name says what it is and where `build_graph` wires it (`build.py` is the only module that connects them).

| Name | What it is |
|---|---|
| `graph_node_<node>` | the function of the node registered as `"<node>"` |
| `make_graph_node_<node>` | the factory that builds it when it needs a client or a setting |
| `graph_node_<node>_bound` | that function with this domain's arguments applied (`functools.partial` in `build_graph`) |
| `graph_edge_route_after_<node>` | the router of `<node>`'s conditional edge; `<node>` is the SOURCE node |
| `graph_edge_route_after_<node>_bound` | the same, with the domain's arguments or the build's finish steps applied |

- **The registered string is the node's identity,** not the function's name: traces, metrics, stream events and checkpoint channels all use `"<node>"`. A function can be renamed freely; the string cannot (a rename is a topology change, so drain the workers first, 16).
- **An edge's stored name is `EDGE_CHANNEL_NAMES[<node>]`,** not its router's name: LangGraph records a conditional edge under its router's `__name__`, which is why renaming a router once stranded turns in flight (16). `build_graph` registers every router through `named_edge` under that table.
- **Adding a node or an edge:** write `graph_node_<node>` in the module that owns the concern, register it with `add("<node>", ...)` in `build_graph`, and for a conditional edge add the router and its table entry. `test_graph_naming.py` fails on a name that breaks the rule; `test_edge_channel_names.py` fails on a table that no longer matches what checkpoints carry.

## Graph Flow

```
START
  ↓
validate_input  ← resets per-turn fields; stamps SecurityCtx from config["configurable"]["ctx"]
  ↓
graph_edge_route_after_validate_input?
  ├─→ reject_context  (no valid tenant+principal, checked FIRST, 17) → END
  ├─→ reject_input    (empty message) → END
  └─→ moderate_input  ← regex screen + Prompt Guard (25): the first spend-adjacent step
       ↓
      graph_edge_route_after_moderate_input?
       ├─→ reject_moderation → END
       └─→ check_semantic_cache  ← tenant+principal+domain cosine-KNN in Redis (22); a failure is a miss
            ↓
           graph_edge_route_after_check_semantic_cache?
            ├─→ check_output   (HIT: no compaction, no retrieval, no LLM)
            └─→ compact_history  ← MISS: trim to FLOOR and fold dropped turns into a summary (41)
                 ↓
                graph_edge_route_after_compact_history?
                 ├─→ context_window_exceeded  (summary too large: a named dead end) → END
                 └─→ route_model  ← writes ONLY `model_route` (68); off by default
                      ║           ┐ one parallel step: neither reads what the other writes (R29)
                      ╚═ retrieve_context  ← hybrid dense+BM25, RRF, rerank, citations (20) + memories;
                      │                      absent when the manifest says prefetch="never"
                      ↓  agent waits for both
                     agent  ← LLM call, context ahead of the question (79); AGENT_RETRY_POLICY
                      ↓
                     graph_edge_route_after_agent?  ← the budgets are checked first, whatever the model said
                      ├─→ no_answer  (iteration, token, cost or repeated-action cap; or the gateway refused, 71) → END
                      ├─→ tool_batch_guard  (too many calls, an unknown tool name, use_skill before skill_search) → agent
                      ├─→ human_approval  (require_approval, OR any non-read_only call: mandatory, 15)
                      │    ↓
                      │   graph_edge_route_after_human_approval?  ← interrupt() paused here
                      │    ├─→ tools      (approved)
                      │    ├─→ agent      (rejected, with synthesized ToolMessages)
                      │    └─→ __end__    (cancelled: checked FIRST, 36)
                      ├─→ tools  ← concurrent; may include remote MCP tools (28) → agent
                      └─→ check_output  ← hits and misses rejoin here (20, 39)
                           ↓
                          graph_edge_route_after_check_output?
                           ├─→ retry_output     (tags the rejected answer + feedback as retry artifacts, 5) → agent
                           ├─→ retry_exhausted  (the SAME reason MAX_CONSECUTIVE_SAME_RETRY_REASON times) → END
                           └─→ finish  (the answer stands: ONE parallel step)
                                ├─→ suggest_followups   ← grounded answers only, never on a hit (27) → END
                                └─→ write_semantic_cache ← unless a hit or a non-read_only tool ran (22) → END
```

A nested `run_subagent` run (46) is the same graph built with `SUBAGENT_PROFILE`, without `check_semantic_cache`, `write_semantic_cache`, `suggest_followups`, `compact_history` and `context_window_exceeded` (and the router).

**20 nodes:** `validate_input`, `reject_input`, `reject_context`, `moderate_input`, `reject_moderation`, `check_semantic_cache`, `compact_history`, `context_window_exceeded`, `route_model`, `retrieve_context`, `agent`, `tools`, `human_approval`, `tool_batch_guard`, `check_output`, `retry_output`, `retry_exhausted`, `no_answer`, `suggest_followups`, `write_semantic_cache`.

**State fields beyond `messages`:** `context`, `context_anchor_index`, `iterations`, `total_tokens`, `total_cost_usd`, `subagent_spend`, `require_approval`, `approved`, `cancelled`, `run_id`, `graph_version`, `state_schema_version`, `ctx`, `citations`, `used_citations`, `ungrounded_claims_count`, `output_findings`, `last_retry_reason`, `retry_reason_repeat_count`, `cache_hit`, `moderation_blocked`, `followups`, `history_summary`, `model_route`.

## Extending Further

### Exactly-once side effects: how the gaps closed

Two kinds of duplicate: a **replay** sees the same `tool_call_id` twice; a **re-ask** arrives under a new `tool_call_id`, which nothing keyed on the id can recognise. The layering is in the [README](../README.md#-safety-model); each row is a defect found and closed.

| Gap | Fix |
|---|---|
| A tool call runs twice (a reclaimed `resume`) | `app/core/tool_idempotency.py::idempotent` wraps every `mutating`/`outward` tool, keyed by `tool_call_id` in `tool_call_dedup`. A second call returns the cached result; resuming re-invokes pending calls under their **original** ids. Fails open (`agent_tool_dedup_degraded_total`) |
| A worker dies mid-turn; a restart would re-ask the LLM for new ids | `reclaim_stale_entries` (`XAUTOCLAIM`) runs in both workers. `_classify_reclaimed_turn` reads the checkpoint, never re-running anything: no `HumanMessage` yet, rerun; its own `HumanMessage` checkpointed and unfinished, **continue** the run (`astream_events_continue_turn`), even after a write, since no new id is minted; finished, approval-paused or unreadable, dead-letter. Capped by `MAX_AUTO_RECLAIM_RETRIES` |
| A finished zero-tool turn is reclaimed before its ack | `reclaimed_turn.turn_already_completed` refuses it (a rejected, tagged answer is not a finished one) |
| Ingest jobs on reclaim | Idempotent by construction (24), retried unconditionally |
| A client resubmits the same message | `claim_or_get_existing_submission` reuses the first attempt's `request_id` within `CHAT_SUBMIT_DEDUP_TTL_SECONDS`; results streams rely on their TTL, since two readers may share one. `release_submission_claim` undoes a claim whose publish failed |
| A tool times out but its write lands | `MutatingToolTimedOutError` steers the agent to verify with a read-only tool first: `asyncio.wait_for` cancels the awaiting task, not necessarily the write, and "retry" would be a fresh id |
| The dedup claim races | `add_note`/`remember` point ids are `uuid5` of `tool_call_id`; `create_ticket`/`log_incident`/`add_followup` carry `tool_call_id UNIQUE` + `ON CONFLICT DO NOTHING` |
| An append to a text column has no row for `ON CONFLICT` | Each append is its own row (`support_ticket_comments`, `crm_lead_notes`); flattened text is computed at read time |
| Nobody ever publishes | `read_results` bounds the wait for the **first** event only (`first_event_deadline_seconds`) |
| One bad upload aborts the batch or orphans a blob | `POST /ingest/upload` returns per-file errors and deletes a blob that failed to publish |
| `tool_call_dedup` grows forever | `make tool-call-dedup-sweep` |
| `usage_ledger` grows forever | Retired; the same question for the events is `make usage-events-sweep` (COST_GOVERNANCE.md 63) |
| Telegram redelivers after a restart | Durable offset in Redis (42) and a `replay_key` per update (89) |

**Deliberately not built:** a business-key rule ("one open ticket per tenant + requester + subject") for two *different* ids used for the same request. What counts as "the same ticket" is a product decision; `MutatingToolTimedOutError`'s steering is the general mitigation.

### Next PR: the LangGraph upgrade (decided, not started; R19)

`requirements.txt` pins `langgraph>=0.2.20,<0.3` (0.2.76 installed). The next PR moves it to **1.0.1**, the highest release this stack can use, as its own PR because it is the only change here with a real compatibility risk.

- **Why 1.0.1 and not 1.2.x:** LangGraph 1.0.3+ needs `langchain-core` 1.x, but the Langfuse **v2** SDK imports `langchain.callbacks`, which langchain 1.x removed. Going further is a ladder (Langfuse server v3 with ClickHouse, then the SDK v3, then langchain 1.x, then LangGraph 1.2.x), not a bump.
- **What it buys:** it clears the import-time "incompatible versions of langgraph and checkpoint-postgres" warning, and nothing else.
- **What to verify, none of it assumed:** a thread **paused** under 0.2.76 resumes under the new version; `test_checkpoint_across_a_topology_change.py` still passes; the `saver.lock = asyncio.Semaphore(...)` workaround in `runtime/core.py` is still needed; `interrupt`/`Command(resume=...)`, `add_messages` replacement by id and parallel fan-out behave as before; `aupdate_state(..., as_node=START)` in `runtime/cancelled_turn.py` (80) still works; and `make test-integration` and `make test-live` pass, not only `make test`.
- **Deploying it:** stop the workers and let in-flight turns finish first (16).

### Not built

- **A fallback node** for the primary LLM path.
- **A vision model that also does tool calling** (44): small local vision models do one or the other; the `vision` alias is a slot, not a verified default.
- **Image-aware moderation, and Telegram/CLI image input.**
- **Restarting a HUNG worker, and autoscaling.** A hung worker goes `unhealthy` and `AgentWorkerLoopStalled` fires (43), but Compose does not restart it (`deploy/README.md`), and nothing scales workers on queue depth, though R17 measures and alerts on it.
- **Per-action authorization within a tenant:** every principal has the write capability the gate allows at all; a `Policy` reading `ctx["claims"]` would live here.
- ~~A Telegram webhook~~ built (93). ~~Real authentication~~ built (84-86, 98). ~~A multi-domain runtime~~ built (96).

### Known gaps

Each notes how it was established; a fix PR deletes it from this list.

- **The small CI models still drop or miss tool calls** (measured with `make probe-tools`, not yet tied to a CI symptom): `qwen2.5:3b` and `1.5b` answered 13/19 and 12/19 against the dev model's 19/19 (73). `agent_unparsed_completion_total` counts the silent form in production.
- **A pre-existing vector collection is trusted, not verified** (67): `qdrant_store.ensure_live` adopts a collection made before embedding identities existed.
- **The ops domain is global:** `ops_incidents` has no tenant dimension by design, any caller can name the domain, and nothing authorizes which tenants may use it. Principle I has no carve-out for it.
- **Approvals are unattributed:** decisions are counted by outcome only (no approver, time or action), so the gate is enforced but not auditable (R12, open).
- **The `tool_call_dedup` lookup is not tenant-scoped:** it assumes provider ids are globally unique, and adding a tenant predicate changes what a collision means (a miss is a second write), so it needs a decision.
- **A residual duplicate window for team-channel posts:** if a second run finds the first's dedup record still in flight, it runs the call. Row-backed writes are protected by uniqueness; the three tools that also post to a channel rely on the conversation lock and on reclaim waiting longer than a turn.
- **Nothing schedules the sweeps** (`make tool-call-dedup-sweep`, `usage-events-sweep`, `checkpoint-sweep`), so those rows accumulate until someone runs them. `checkpoint-sweep` deletes a conversation `CHECKPOINT_RETENTION_DAYS` after its last message; **0, the default, is off** (`--dry-run` counts and changes nothing). It does not trim a LIVE conversation and does not touch spend, memories, the gateway's logs or Langfuse.
- **A person can delete their own conversation (R11):** `DELETE /chat/sessions/{thread_id}` checks ownership first (another person's thread and an unknown one are the same 404), answers `409 thread_busy` while a worker holds the lock, then deletes the checkpoints and the row. A message sent in the moment between the check and the delete starts a new conversation under the same id. Langfuse and gateway copies remain, and the web page has no button.
- **Memory deletion has no entry point:** `delete_memories` is built and tested, but nothing calls it (33).
- **Tenant isolation is only partly proven against real backends:** documents and the cache's tenant axis hit real Qdrant and Redis; memories, relational stores, sessions and the cache's principal axis rely on hermetic tests.
- **`reclaim_stale_entries` is mostly tested against a fake** that is not paginated like real `XAUTOCLAIM`; one integration test shows a real `XAUTOCLAIM` still finds an unacknowledged job after the age trim.

**The key insight:** LangGraph lets you make every step of the pipeline explicit and controllable. That is what separates it from "just LLM + tools."
