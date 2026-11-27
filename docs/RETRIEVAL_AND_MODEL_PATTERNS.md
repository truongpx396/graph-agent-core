# Retrieval and model patterns

What the agent knows (documents, memories, embeddings) and which model answers. The nodes that use them are in [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md); the spend side is [COST_GOVERNANCE.md](COST_GOVERNANCE.md). Numbers are shared with the other pattern documents ([index](README.md#pattern-numbers)).

| # | Lesson |
|---|---|
| 18 | Cross-session memory |
| 20 | Hybrid retrieval, rerank and cited answers |
| 24 | General-purpose ingestor with parent-child chunking |
| 33 | Memory deletion audit, age selector, retention at recall |
| 38 | Resolved model recording |
| 67 | Embedding roles and identity |
| 68 | A model-routing seam that can only advise |
| 73 | The chat model is chosen by what it does with this app's tool calls |
| 74 | Strict tool schemas are a form the model is sent, and they are off |

### 18. Cross-session memory
`remember` is the only way a memory is written (`mutating`, gated like `add_note`). Recall is automatic: the model decides to *write*, never to *read*. The memory leg of `gather_context` re-applies `Policy.lower` on every call, scoped to tenant and owner, never cached. Memories are framed like retrieved documents, and a poisoned one replays every turn until removed.
- **Not built:** an LLM-facing forget tool (`delete_by_filter` serves a data-subject-request script).

### 20. Hybrid retrieval, rerank and cited answers
`hybrid_search()` (`qdrant_store.py`) runs two Qdrant legs (dense via LiteLLM, BM25 sparse via in-process `fastembed`) fused with RRF, then reranks with a cross-encoder in the `ml-service` container. Every hit is numbered `[1]`, `[2]`...; `check_output` writes only the markers the answer actually contains, **never trusting the model's claim about what it cited**.
- **Degrades in two layers:** sparse failure falls back to dense-only, rerank failure returns the RRF order.
- **A citation links to the original** when the chunk names one (`source_url`, written only by ingestion, plain http(s) only). It goes in the citation record, never in the prompt, so the model has no address to copy or invent.
- **Bug:** the search was cut to `RERANK_TOP_K` before sibling chunks of one parent were removed, so one long passage's overlapping children could take every place. All reranked candidates are deduplicated first, then cut (they are all scored either way).
- **Performance fixes:** one Qdrant client per event loop (R03); payload indexes on `tenant`, `kind`, `owner`, `topic`, `source`, `created_at` (an existing deployment runs `make qdrant-indexes` once, R09); one shared ML-service HTTP client and a 300 s `embed_query` cache that shares concurrent requests (R10); retrieval runs in parallel with the router, and `AgentManifest.prefetch="never"` leaves it out of a domain's graph (none does today; R29).

### 24. General-purpose ingestor with parent-child chunking
`ingest_text`/`ingest_file`/`ingest_url` and `make ingest` share one chunk, embed, `build_point` pipeline (`app/ingestion/`); every item carries its tenant. Parents (~1200 chars) go to the prompt, overlapping children (~600) are embedded. Point ids derive from `(tenant, source, chunk index, content)`, so re-ingesting identical content upserts.
- **SSRF guard:** `https` only, every resolved address globally routable, no redirects.
- **Provenance on every chunk:** `title` (a fetched page's own `<title>`), `source` (the document's identity), `source_url` (where a person reads it, when there is one) and `chunking` (how it was cut). A re-embed keeps chunks as they are, so after a chunking change `chunking` says what needs ingesting again; it can't be added afterwards.
- **A short document is one chunk**, with `text` equal to `parent_text`. Every sample document used to be that short, so the demo data never showed parent-child at all; four are now summaries of real documentation pages that split into several parents and children.
- **Bug:** the first guard was a deny-list and missed `100.64.0.0/10` (where cloud metadata lives). The fetcher also let the HTTP client resolve again after checking (a rebinding window) and read the whole body before the size check. It now connects to the address it checked and streams to the limit (R20).
- **Bug:** re-uploading an EDITED document left the old chunks citable. An ingest with an `ingest_id` writes its own id space, then removes the tenant's chunks of that source from strictly OLDER ingests. "Delete whatever has a different id" was wrong: two simultaneous uploads deleted each other. `DELETE /ingest/documents` removes a document and its stored files (R18).
- **Bug:** a 78 KB uploaded PDF made `pypdf` 5 inflate 80 MB. pypdf 6 stops it with `LimitReachedError`, which is not a `PdfReadError`, so the extractor catches the base `PyPdfError`: a bad file is a failed job, never an unexpected exception.
- **Gaps:** an uploaded file's citation has no link (no route serves the original); child windows are cut at a character count, so they can start mid-word, and a child carries neither its document's title nor its section; an older upload that finishes last keeps its chunks until the next upload; the cache may quote old text for its TTL; nothing lists a tenant's documents. The **crawler's** rebinding window is not closed; its containment is the network (`deploy/README.md`).

### 33. Memory deletion audit, age selector, retention at recall
`delete_memories` requires **exactly one** selector (an ambiguous request is refused, not narrowed to "everything"), counts matches first and records every call. `Policy.lower` also ANDs a `DatetimeRange` (`MEMORY_RETENTION_DAYS`, 365) onto every recall, so an unswept expired memory is invisible.
- **Gap:** nothing calls `delete_memories` (no script, endpoint or make target).

### 38. Resolved model recording
`resolve_model` (`app/adapters/outbound/litellm/model_catalog.py`) asks LiteLLM's `GET /model/info` which concrete model serves an alias and records it as `resolved_model` on each usage event, so a silent remap behind an alias is visible.
- **Bug:** the answer was kept for the process life, so a remap was visible only after a restart. It is trusted for `MODEL_RESOLVE_TTL_SECONDS` (60); a refresh that fails keeps the last answer (see pattern 67 for what it cost retrieval).
- **Bug:** the first version was a sync `httpx.get` on the event loop at the end of every turn. It is `async` and remembers a failure for 60 s per alias.

### 67. Embedding roles and identity: a question is not a passage, and the same size is not the same space
Changing what you embed changes the space, and Qdrant cannot tell: two 768-d models accept each other's queries and return confident nonsense.
- **Two roles:** `embed_query` / `embed_document(s)` replace `embed_text`; an AST test fails if anything calls the raw client around them. `EMBED_QUERY_PREFIX` / `EMBED_DOCUMENT_PREFIX` default to empty; whether a prefix helps is measured (`make model-eval`).
- **Identity in the collection's name:** (model, vector size, both prefixes, `EMBED_MODEL_REVISION`) hashed into the physical name (`docs-1a2b3c4d`). Every read and write goes through the alias `docs-live` and compares the suffix (`qdrant_store.check_identity`). A mismatch is **refused**: a read degrades, a write fails, `EmbeddingIdentityMismatch` fires.
- **The swap:** `make reembed COLLECTION_NAME=docs` builds the new collection beside the live one from each point's stored text, verifies it and repoints the alias in ONE request; the old one is never deleted. The answer cache's index carries the hash too. `/v1/embeddings` on the ML service speaks the OpenAI shape, so a model swap there is configuration only.
- **Readable by a person:** 8 hex digits say whether two collections match, not what either holds. The collection keeps the identity as its metadata (Qdrant 1.16+), and `make vector-collections` prints it beside the configured one. Per chunk would be redundant: one collection holds one identity.
- **Bug:** re-pointing the `embed` alias at the gateway, with the app left running, went unnoticed (the resolved model was cached for the process life, pattern 38), so new-model queries searched old-model vectors. The app now re-asks within the TTL and drops the dimension and query vectors it measured with the old model. Up to that long after a re-point it still believes the old model, so switch by adding a second alias (`deploy/README.md`).
- **Bug:** neither production nor dev compose passed the prefixes or `EMBED_MODEL_REVISION` to a container (production also fixed `EMBED_MODEL`), so setting them in `.env` changed nothing there.
- **Gaps:** a switch is atomic but not zero-downtime (no dual writes, refused from the swap to the app's restart, going back rebuilds); nothing gates the swap on retrieval quality (`make model-eval` is run by hand); `--record` can describe only a collection whose name matches the current settings; a pre-identity collection is *adopted* and trusted, not verified, until its first re-embed; where LiteLLM cannot name the model, the alias name stands in (`unverified`; `EMBED_MODEL_REVISION` covers the same name re-pulled); writes during a re-embed are detected, not lost (exit 4); the default is still nomic through Ollama with prefixes off.

### 68. A model-routing seam that can only advise, from two signals and in three tiers
An operator can attach up to two more models behind gateway aliases (`chat-medium`, `chat-strong`). The router picks a tier per turn from rules over the message (`app/core/routing_policy.py`) and a label from the ML service's `route-topics` set (`POST /v1/decide`): `min(ceiling, max(rules, label))`, lowered to the highest tier that is configured and priced.
- **The risk is a second authority.** The router writes `State.model_route` and nothing else; one test pins the single key, another runs the same mutating turn under all three tiers and asserts the same approval pause, tenant-scoped payload and `idempotent()` key.
- **Where it sits:** after moderation, cache and compaction, in parallel with retrieval; never for a refused input, a cache hit, a resumed turn or a subagent.
- **It sends** the first 2,000 characters of the current message, never tenant, principal, history or retrieved text. The answer is validated, and a missing label never cancels the rules.
- **Modes:** `off`, `shadow` (decides and counts, runs local) and `on`; `routing_policy.decide` is pure, so the evaluation harness runs the same code.
- **A failing routed model is handled in `agent()`, not the gateway:** the call repeats on the local alias (`ModelRoutedTierFailing`), because a gateway `fallbacks` entry would price a model that did not serve. An unpriced routed alias steps down a tier, so the router is never why a turn is refused.
- **In dev both tiers are free-tier Gemini** (`GOOGLE_API_KEY`, with `num_retries: 0` so the gateway does not multiply the app's retries against a daily quota); in production both aliases default to `chat` until `LLM_MEDIUM_*` / `LLM_STRONG_*` are set. **Not built:** per-tenant control (set `ROUTER_MAX_TIER=local`).
- **Gaps:** nothing shows routing improves an answer (the label set itself is measured, `make model-eval KIND=classifier ... DATASET=route-topics`); only the current message is read; a routed endpoint that never answers is not seen by the fallback; a routed alias that is **out of quota** (429) fails the turn, since only an unreachable model or a 5xx repeats on `chat`.

### 73. The chat model is chosen by what it does with this app's tool calls, and a sampling option can break them
"Remember this" never ran `remember`: Ollama silently discards a tool call whose JSON it cannot parse. Two causes: the gateway's `repeat_penalty: 1.3` (it penalises the `":` and quote tokens a call repeats) and `qwen2.5:3b` being unreliable even without it. `make probe-tools` (22 labelled prompts) scored tool-needing prompts answered, out of 19:

| repeat_penalty | `qwen2.5:3b` | `qwen2.5:7b` | `qwen2.5:1.5b` |
|---|---|---|---|
| 1.3 (old) | 8 | 11 | 5 |
| 1.1 (default) | 13 | **19** | 12 |
| 1.0 | 15 | **19** | 12 |

- **Decision:** `chat` and `chat-backup` are `qwen2.5:7b` at `repeat_penalty: 1.1`. Run `make probe-tools` after ANY change to a local chat alias's model or `repeat_penalty` (it exits 1 on a dropped call, a loop, or under 90%).
- **Gaps:** 22 prompts on one machine is a screen, not a ranking; CI's small models (3b, 1.5b) score 13/19 and 12/19, a plausible cause of flaky live tests, not yet tested; the `vision` alias keeps `repeat_penalty: 1.3`.

### 74. Strict tool schemas are a form the model is sent, not the contract the app enforces, and they are off until a provider enforces them
`TOOL_CALLS_STRICT` makes `make_llm` send every tool in OpenAI strict form (`strict_tool_schema`, `app/core/tool_schema.py`). It is **off**.
- **LangChain's `strict=True` does not make a tool strict-valid:** it closes objects and sets the flag, nothing else. `strict_tool_schema` lists every property as required (optional ones nullable), closes every object and moves defaults and bounds into the description; `tests/core/test_tool_schema.py` checks every tool of every domain.
- **A missing nullable argument is filled with `None` before validation:** `qwen2.5:7b` sends only the arguments it needs, so refusing that failed 7 of 19 probe prompts and no prompt wording fixed it.
- **Why off:** LiteLLM's `ollama_chat` hands `tools` to Ollama untouched, and the strict form costs a little (7b 19 to 18, 3b 14 to 12, 1.5b 9 to 8). Turn it on with the change that points `chat` at a provider that enforces it, running `make probe-tools` before and after.
- **Gaps:** never run against a strict provider; strict output is not guaranteed with parallel tool calls, which the graph supports.
