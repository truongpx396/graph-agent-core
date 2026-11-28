# Implementation Plan: a runtime contract an agent platform can drive

**Spec**: [spec.md](spec.md) · **Source**: [AGENT_PLATFORM_ALIGNMENT_PLAN.md](../../docs/AGENT_PLATFORM_ALIGNMENT_PLAN.md), which has the shapes of each event and the phase table. This file records only what changed or was decided since.

## Decisions (2026-11-28)

| Question | Decision | Why |
|---|---|---|
| The plan says every change is additive and a minor version | **One major version, 4.0.0**, declaring `status`, `usage`, `system_note` and `approval_superseded` at once | Measured with oasdiff 1.35.0: a new member of the event union is `response-property-one-of-added` and a new error code is `response-property-enum-value-added`, both errors that need a major version. Optional fields pass. Loosening the gate would have hidden the same change in every other document |
| `usage` and `approval_superseded` are declared before they are sent | Accepted | The alternative is a second and third major version |
| Open vocabularies | `status.state` and `usage.route` are strings with `x-extensible-enum`, not enums | A value added to a response enum is another major version |
| Spec number | 025 | 024 was taken by distributed tracing after the plan was written |
| Delivery | One PR at a time, reviewed and merged before the next | |

## Step 1: the contract and the `status` event

- **Models** (`api/events.py`): `StatusEvent`, `UsageEvent`, `SystemNoteEvent`. `system_note` has been sent since pattern 16 and was never in the document.
- **Producer** (`outbound/redis/queue.py`): a turn or resume writes the job, its owner and `queued` in one `MULTI`. A worker cannot read the job before the transaction applies, so `queued` is always first, and a refused write leaves neither.
- **Worker** (`inbound/workers/agent_worker.py`): `started` is published after the stale cancel flag is cleared, never before: a client may press stop as soon as it is told, and a later clear would erase that.
- **Worker label**: `w-<random>`, the suffix of the consumer name without the host name, which a caller must not learn (pattern 30).
- **Deadline** (`queue.py::read_results_from`): ends at the first event that is not `queued`. A reader resuming from a position asks the stream whether a worker ever wrote to it: before this change no position existed until a worker had written one.
- **Channels**: all three ignore a type they do not know. The door now says the connection was lost, not that nothing was sent, once the API has answered with an event.

## The rest of phase 1

| Decision | Why |
|---|---|
| Tool events are announced by the `tools` node (`graph/tool_node.py`), not read from `astream_events` | Its tool callbacks carry no call id, and a raising tool ends no event |
| `ToolDisplay` in a `display.py` per domain, on the `DomainPlugin` port | Beside the tools, which are already large files; the plan's `(tier, label)` tuple would have changed every reader of the capabilities |
| `approval_id` is a digest of the pending calls, not a random id | The pausing node runs again on resume |
| A resume with no id answers the pause the worker first found for that job | A reclaimed resume could otherwise approve a later pause (Principle II) |
| `usage` is summed from a new `State.turn_usage` | It must span a pause; the running totals cannot say "unknown". No `STATE_SCHEMA_VERSION` bump: an old checkpoint resumes with an empty list |
| `details.kind` is added in `ErrorEnvelope.to_dict` | One place. Not on `cancelled`, nor on the four codes the front door also answers |
| A stop holds the time it was pressed; the worker compares it with the job's `enqueued_at` | Both are the API's clock, and the order survives a reclaimed job |

Reversibility is reported as `false` for every write: this app has no tool that undoes one.

## Later steps

As the source plan's section 8, with these differences: every PR after step 1 is a minor version (phase 1 ends at 4.1.0); the web phases target `web/` (a `web/DESIGN.md` and a vitest guard replace the `static/` ones, and the ES-module split no longer applies).

## Constitution Check

- **I, tenant isolation**: step 1 adds no read path; the stream's owner check is unchanged. The runtime card and `/chat/approvals` (later) are scoped to the caller.
- **II, mandatory approval**: nothing approves. Later flags (`read_only`) only restrict; `approval_id` narrows what an approval applies to.
- **III, fixed tools**: no tool changes in step 1. Tool display metadata (later) is code beside each tool, never model output.
- **IV, exactly-once**: no write tool is added. `queued` is part of the publish transaction, so a failed publish leaves nothing to dedupe. `approval_id` (later) makes a resume single-use for one pause.
- **V, bounded and observable**: the first-response deadline now also covers a reader that saw only `queued`. A failed `started` write fails the job before anything runs, as any publish failure does.
- **VI, untrusted content**: status carries no outside text. Later labels and summaries are capped and scrubbed.
- **VII, tests**: hermetic tests for the order, the deadline and the stop race; the real-Redis tier for the transaction.
- **VIII, docs**: RUNTIME_PATTERNS.md 117, the turn and approval diagrams, README.
