# Feature Specification: a runtime contract an agent platform can drive

**Feature Branch**: `feat/runtime-status-event` (step 1, #317), `feat/runtime-tool-approval-usage` (the rest of phase 1)

**Created**: 2026-11-28

**Status**: In progress. Phase 1, the contract, is built; [tasks.md](tasks.md) says what is left.

**Input**: "pls apply this plan": [AGENT_PLATFORM_ALIGNMENT_PLAN.md](../../docs/AGENT_PLATFORM_ALIGNMENT_PLAN.md), Parts A and C, with Part B's web UI work as later phases.

## Why

Almost everything a good agent UI or an agent platform needs is a fact the runtime already knows and does not send: whether a worker has taken the turn, which tier a tool is, what a call did, what the turn cost. Each client would otherwise guess, and guess differently.

**The stance: the runtime publishes facts, every client renders them.** The web UI, the Telegram bot, the CLI and any platform read one event vocabulary. No client computes risk, labels or state of its own.

## User Scenarios & Testing

### User Story 1 - "Waiting for a worker" is not "thinking" (Priority: P1)

A person who sent a message, or a platform that admitted a job, can tell whether anything has picked it up.

**Independent Test**: with no worker running, a turn's stream says it is queued and never that it started; with one running, it says started before any text.

1. **Given** no worker serves the domain, **When** a turn is sent, **Then** the stream says `queued` and ends with `worker_unreachable` after the first-response deadline.
2. **Given** a worker is free, **When** a turn is sent or an approval is answered, **Then** the stream says `queued`, then `started`, before any other event.
3. **Given** a person presses stop as soon as the stream says `started`, **Then** the turn stops.
4. **Given** a person presses stop while the turn is still queued, **Then** nothing of it runs when a worker takes it.

### User Story 2 - A decision is about exactly what was shown (Priority: P1)

An approval names the action, its effect, its target and whether it can be undone, and approving it cannot approve anything else.

**Independent Test**: answer a pause with the id of an earlier pause and see it refused with nothing run.

1. **Given** a paused turn, **When** the request is shown, **Then** it has a title, an effect in words, a tier and whether it is reversible; an action that cannot say is shown as not reversible.
2. **Given** a double click, an old tab or a replayed Telegram button, **When** it answers a pause that has been replaced, **Then** it is refused (`approval_superseded`) and the current pause stays.
3. **Given** a reloaded page, **When** it reads the pending approval, **Then** it gets the same request and id.

### User Story 3 - What the agent did, in words (Priority: P2)

Each tool call can be followed from start to end: a label, its tier, whether it worked or was refused, how long it took, and one line of result.

**Independent Test**: two parallel calls of one tool are paired correctly by their call ids.

### User Story 4 - Cost and kind of failure (Priority: P2)

Each turn says which model answered and what it used; a refusal by policy or budget reads differently from a failure.

**Independent Test**: a turn on an unpriced model reports its cost as unknown, not zero; a budget refusal is marked `blocked`.

### User Story 5 - A platform discovers and drives the runtime (Priority: P3)

A platform asks what this runtime can do, runs a turn that may only read, and lists everything waiting for a person.

**Independent Test**: the runtime card lists each domain's tools with their tiers and reports `steer: false`; a read-only turn is never offered a tool that writes.

### User Story 6 - The integration stays true (Priority: P3)

A platform adapter is written against recorded turns and one guide, and a change that alters a sequence fails a test.

### Edge Cases

- A worker crashes and the job is run again: `started` appears again on the same stream.
- A conversation is busy: the stream says `queued`, then `thread_busy`, and never `started`.
- A client reconnects having seen only `queued`: the wait for a worker is still bounded.
- A worker older than the API runs the job (rolling deploy): it sends no `started`, and its first event counts as one.
- A client written before an event type existed: it ignores a `type` it does not know.

## Requirements

### Functional Requirements

**Contract**

- **FR-001**: Every client reads the same events, described by the committed OpenAPI document. A client ignores an event `type` or an error `code` it does not know.
- **FR-002**: Version 4.0.0 declares every new event type (`status`, `usage`, `system_note`) and the new error code (`approval_superseded`) at once. Later phases add only optional fields and new routes.
- **FR-003**: Free text the runtime adds to a stream (labels, summaries, effects) has a length cap and is scrubbed of credentials.
- **FR-004**: A value the runtime cannot determine is null, never zero.

**Status (story 1)**

- **FR-010**: A turn or an approval answer says `queued` when it is accepted. It is written together with the job, so it exists only if the job does and precedes everything a worker writes.
- **FR-011**: The worker that takes the job says `started` before anything runs, with the time and an opaque worker label that names no host.
- **FR-012**: `worker_unreachable` is measured from the absence of a worker's event, not of any event, also when a client reads on from a position.
- **FR-013**: A cancel has no status: it is a control request, answered by its outcome.

**Approvals (story 2)**

- **FR-020**: A pause carries one item per call (call id, tier, title, effect, target, who sees it, reversible) and an id of that pause.
- **FR-021**: A resume that names a stale or unknown id is refused and runs nothing. A resume that names none still works, for older clients.
- **FR-022**: Approval stays all-or-nothing, and there is no "edit" until the runtime can honour one.

**Tools, usage and errors (stories 3 and 4)**

- **FR-030**: Tool events carry the call id, label, tier, outcome (`ok`, `error`, `refused`), duration and an optional summary.
- **FR-031**: A usage event before `done` reports model, route, tokens, cost and model calls, subagents included.
- **FR-032**: Errors and refused tool calls say their kind: `blocked`, `failed` or `lost`.

**Discovery and control (story 5)**

- **FR-040**: `GET /v1/runtime` describes the domains, their tools and tiers, and what the runtime supports, including what it does not.
- **FR-041**: A `read_only` request flag offers the model only read-only tools. Request flags can only make a turn stricter.
- **FR-042**: `GET /chat/approvals` lists the caller's own pending approvals, scoped to tenant, person and domain.

**Never**

- **FR-050**: No auto-approve, "allow for this session" or full-access mode for a mutating or outward tool.
- **FR-051**: No caller-settable tenant or "on behalf of" field.

### Key Entities

- **Status**: where a job is (queued, started), when, and which worker.
- **Approval request**: one pause: its id and its items.
- **Runtime card**: the static description a platform reads before it drives the runtime.

## Success Criteria

- **SC-001**: With no worker running, a client can say "waiting to start" instead of showing work in progress, for the whole wait.
- **SC-002**: No approval can be applied to a request other than the one shown.
- **SC-003**: A platform adapter needs no knowledge of this codebase beyond the OpenAPI document, the runtime card and the integration guide.
- **SC-004**: The web UI shows no tool identifier or raw JSON by default.

## Assumptions

- Discovery is at `GET /v1/runtime` (versioned), not `/.well-known/`.
- The first integration target is the owner's own platform; its adapter lives in that platform's repository.
- The web UI work is built in `web/` ([specs/023](../023-web-client/plan.md)), not in the page the plan first named.
- A run API with callbacks (the plan's A9) waits for a consumer that needs it.

## Not included

- Steering a running turn and queued follow-ups: reported as unsupported.
- A task board, run timeline or org chart: those belong in the platform.
- The web page sends no `approval_id` and shows none of the new facts until phase 2.
