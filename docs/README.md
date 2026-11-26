# Documentation

The repository's [README](../README.md) is the front page: what it is, how to run it, the HTTP API, deploying. This folder holds the design and reference documents behind it.

## Start here

| Read | When you want to know |
|---|---|
| [ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md) | The whole system as Mermaid maps: processes, layers, the graph, and a sequence diagram for each critical flow. The best first read |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The layering rules: rings, ports and adapters, what may import what, how to add a port, and the tests that enforce it |
| [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) | Why the agent graph is built the way it is, its flow, and what is and is not built |

## Patterns, by concern

Each pattern states the mechanism, the real bug behind it and the gap still open. They share one number sequence ([below](#pattern-numbers)).

| Read | Patterns | About |
|---|---|---|
| [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) | 1-10, 12-17, 19, 22, 23, 25, 27, 34-36, 39, 41, 44-47, 70, 71, 79-81, 104, 105 | The graph: loop, approval, cancellation, context and cache nodes, checkpointing, composition |
| [RUNTIME_PATTERNS.md](RUNTIME_PATTERNS.md) | 11, 29-31, 37, 43, 49, 96, 99 | The queue, the workers, pools, metrics, the error envelope, the web UI |
| [ACCESS_PATTERNS.md](ACCESS_PATTERNS.md) | 82, 84-86, 97, 98, 103 | The front door, signed identity, revocation, the MCP gateway, production routing |
| [CHANNEL_PATTERNS.md](CHANNEL_PATTERNS.md) | 42, 89, 91-94 | The CLI and Telegram as clients of the chat API |
| [TOOL_PATTERNS.md](TOOL_PATTERNS.md) | 21, 28, 32, 50, 78, 87 | Data tools, MCP in and out, scrubbing, crawl and sandbox, web search |
| [RETRIEVAL_AND_MODEL_PATTERNS.md](RETRIEVAL_AND_MODEL_PATTERNS.md) | 18, 20, 24, 33, 38, 67, 68, 73, 74 | Memory, retrieval, ingestion, embeddings, model choice and routing |
| [COST_GOVERNANCE.md](COST_GOVERNANCE.md) | 26, 51, 57, 58, 62-64, 74 | How spend is priced, metered and capped; the gateway backstop |
| [ENGINEERING_PATTERNS.md](ENGINEERING_PATTERNS.md) | 40, 48, 59, 60, 69, 72, 75-77, 83, 86-88, 90, 95, 100-102, 106, 107 | Settings, tests, builds and measurements |
| [`packages/billing/docs/PATTERNS.md`](../packages/billing/docs/PATTERNS.md) | 52-56, 61, 65, 66 | The credit wallet, webhooks, export, reconciliation |

## Reference and records

| Read | What it is |
|---|---|
| [WORKER_CONCURRENCY.md](WORKER_CONCURRENCY.md) | How the queue workers scale and how to size them |
| [BILLING_INTEGRATION.md](BILLING_INTEGRATION.md) | The app's side of the billing seam (the package's own docs are in `packages/billing/docs/`) |
| [PRODUCTION_REVIEW.md](PRODUCTION_REVIEW.md) | The 2026-11-13 review as work orders (R01 to R35). Most are done; R12, R19 and R34 are open. It holds the evidence the pattern files leave out |
| [AGENT_PLATFORM_ALIGNMENT_PLAN.md](AGENT_PLATFORM_ALIGNMENT_PLAN.md) | A plan (not yet built) to make the runtime and web UI drivable by an agent platform |
| [AGENT_GRAPH_FIXES.md](AGENT_GRAPH_FIXES.md) | A record of the 2026-10-15 graph review. Finished; it can be deleted |

## Pattern numbers

Code and tests cite a pattern by number ("pattern 43"), so a number is **never reused, moved or renumbered**. A pattern that leaves a document leaves a row in [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md#where-the-other-numbers-live), so a comment saying "GRAPH_PATTERNS.md pattern 43" still finds its way to [RUNTIME_PATTERNS.md](RUNTIME_PATTERNS.md); the table above is the full map.

- A bare number means the document you are reading. Cite another document's pattern with its file: `COST_GOVERNANCE.md 74`, `ENGINEERING_PATTERNS.md 86`.
- Three numbers were each used twice before this rule existed and cannot be changed now: **74** (strict tool schemas, and the gateway's view of spend), **86** (the API's identity, and smoke checks) and **87** (web search, and live tests).
- The next free number is **108**.

## Elsewhere

- [`.specify/memory/constitution.md`](../.specify/memory/constitution.md): the non-negotiable rules.
- [`specs/`](../specs/): one folder per feature (Spec Kit).
- [`deploy/README.md`](../deploy/README.md): the production runbook. [`evals/README.md`](../evals/README.md): model evaluation.
- [`CLAUDE.md`](../CLAUDE.md): the map of the code for agents and newcomers.

## Writing a document here

Say what the code does and why, not how the change was reached. Keep a pattern to the mechanism, the failure that motivated it (one line) and the gap still open; put long evidence in a work order or a spec and link to it. A file or function a document names must exist: `tests/core/test_doc_references.py` checks the main documents. A Mermaid diagram must render: `;` inside a sequence-diagram message ends the statement and breaks the whole diagram.
