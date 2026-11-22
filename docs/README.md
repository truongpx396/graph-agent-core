# Documentation

The repository's [README](../README.md) is the front page: what it is, how to run it, the HTTP API, deploying. This folder holds the design and reference documents behind it.

## Start here

| Read | When you want to know |
|---|---|
| [ARCHITECTURE_DIAGRAMS.md](ARCHITECTURE_DIAGRAMS.md) | The whole system as Mermaid maps: processes, layers, the graph, and a sequence diagram for each critical flow. The best first read |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The layering rules: rings, ports and adapters, what may import what, how to add a port, and the tests that enforce it |
| [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) | Why the agent graph is built the way it is: numbered patterns, each with the real bug behind it, plus what is and is not built |

## Reference

| Read | When you want to know |
|---|---|
| [COST_GOVERNANCE.md](COST_GOVERNANCE.md) | How spend is priced, metered and capped; the gateway backstop |
| [ENGINEERING_PATTERNS.md](ENGINEERING_PATTERNS.md) | Lessons about this repository's settings, tests, builds and measurements |
| [WORKER_CONCURRENCY.md](WORKER_CONCURRENCY.md) | How the queue workers scale and how to size them |
| [BILLING_INTEGRATION.md](BILLING_INTEGRATION.md) | The app's side of the billing seam (the package's own docs are in `packages/billing/docs/`) |

## Work in progress and records

| Read | What it is |
|---|---|
| [PRODUCTION_REVIEW.md](PRODUCTION_REVIEW.md) | The 2026-11-13 review as work orders (R01 to R35). Most are done; R12, R19 and R34 are open. It holds the evidence the pattern files leave out |
| [AGENT_GRAPH_FIXES.md](AGENT_GRAPH_FIXES.md) | A record of the 2026-10-15 graph review. Finished; it can be deleted |

## Pattern numbers

Four documents number their lessons from **one sequence**: [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md), [COST_GOVERNANCE.md](COST_GOVERNANCE.md), [ENGINEERING_PATTERNS.md](ENGINEERING_PATTERNS.md) and the billing package's [PATTERNS.md](../packages/billing/docs/PATTERNS.md). Code and tests cite a pattern by number ("pattern 43"), so a number is never reused, moved or renumbered, and a pattern that leaves a document leaves a stub behind.

- **A bare number is a graph pattern.** Cite one from another document with its file: `COST_GOVERNANCE.md 74`, `ENGINEERING_PATTERNS.md 86`.
- Three numbers were each used twice before this rule existed (74, 86, 87) and cannot be changed now.
- The next free number is **103**.

## Elsewhere

- [`.specify/memory/constitution.md`](../.specify/memory/constitution.md): the non-negotiable rules.
- [`specs/`](../specs/): one folder per feature (Spec Kit).
- [`deploy/README.md`](../deploy/README.md): the production runbook. [`evals/README.md`](../evals/README.md): model evaluation.
- [`CLAUDE.md`](../CLAUDE.md): the map of the code for agents and newcomers.

## Writing a document here

Say what the code does and why, not how the change was reached. Keep each pattern to the mechanism, the failure that motivated it (one line) and the gap that is still open; put long evidence in a work order or a spec and link to it. A file or function a document names must exist: `tests/core/test_doc_references.py` checks the main documents. A Mermaid diagram must render: `;` inside a sequence-diagram message ends the statement and breaks the whole diagram.
