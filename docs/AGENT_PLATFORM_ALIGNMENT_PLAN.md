# Alignment plan: graph-agent-core as a platform-ready agent runtime

What has to change in graph-agent-core (the API contract and `static/index.html`) to match what
the agent-platforms UI/UX research (a separate document, not kept in this repository) found. The goal is also that
an agent platform (a new one you build, or Paperclip, Multica and the like) can drive this runtime
the same way it drives Claude Code, Codex or OpenCode.

Written 2026-11-22, against `main` at `49f2d69` (API 3.4.0).

**Update 2026-11-28:** being built as [specs/025](../specs/025-agent-runtime-contract/plan.md), one pull request at a time. Three things below no
longer hold, and the spec's plan has the reasons: the spec number is 025 (024 was taken); rule 1's "each change is a minor version" is not possible
for a new event type or error code, which oasdiff reports as breaking, so **4.0.0 declares them all at once**; and the web work is in `web/`.

---

## Contents

1. [Goals and the stance behind them](#1-goals-and-the-stance-behind-them)
2. [Where things stand today](#2-where-things-stand-today)
3. [Rules for every change in this plan](#3-rules-for-every-change-in-this-plan)
4. [Part A: the runtime contract (backend)](#4-part-a-the-runtime-contract-backend)
5. [Part B: the web UI (`index.html`)](#5-part-b-the-web-ui-indexhtml)
6. [Part C: integration readiness](#6-part-c-integration-readiness)
7. [Part D: blueprint for the new platform](#7-part-d-blueprint-for-the-new-platform)
8. [Phasing and PR plan](#8-phasing-and-pr-plan)
9. [Constitution, docs and tests](#9-constitution-docs-and-tests)
10. [Decisions for you](#10-decisions-for-you)

---

## 1. Goals and the stance behind them

**Goal A: align.** Make the web UI show agent work the way the best platforms do: an approval card
that names action, data and risk; a collapsible activity rail; honest queued and waiting states;
cost on each turn; one list of everything waiting on you.

**Goal B: be drivable.** Make graph-agent-core a well-behaved *runtime* that any platform can host
next to other runtimes. In Paperclip's words, *"if it can receive a heartbeat, it's hired"*.

**The stance that connects the two.** Almost everything the UI needs is a *fact the server
already knows but doesn't send*: which tier a tool is, whether a worker has picked the turn up, what
a turn cost, what a tool call actually did. So the plan is backend-first:

> **The runtime publishes facts; every client renders them.** The web UI, the Telegram bot, the CLI
> and any future platform consume the *same* event vocabulary. No client computes risk, labels or
> state on its own.

If that holds, the web UI improvements and platform integration come from one piece of work, not
two. It also settles where the rich product UI should live. graph-agent-core's `index.html` stays a
thin, honest **reference client and operator console**. The rich product experience (inbox, task
board, org chart, run timelines) belongs in the new platform, which talks to graph-agent-core
through the contract in Part A.

---

## 2. Where things stand today

Facts from the code, each one a gap the research points at:

| Area | Today | Gap against the research |
|---|---|---|
| **Endpoints** | `POST /chat/stream/queued`, `GET /chat/stream/{request_id}` (re-read with `Last-Event-ID`), `POST /chat/resume`, `POST /chat/cancel`, `GET /chat/sessions`, `GET /chat/sessions/{id}/messages`, `GET /chat/sessions/{id}/pending_approval`, `POST /chat/feedback`, `GET /usage`, ingest routes, health | No way to *discover* what the runtime can do; no list of approvals across sessions |
| **Event vocabulary** (`app/adapters/inbound/api/events.py`) | `token`, `tool_start{tool,args,subagent}`, `tool_end{tool,subagent}`, `retry`, `compacted`, `citations`, `followups`, `approval_required{tool_calls[{name,args}]}`, `done`, `error{code,message,details}` | Good bones (closed set, discriminated by `type`, tested against the committed document). Missing: a status event, tool call identity and outcome, usage, risk on approvals |
| **Queued vs running** | The API publishes to Redis Streams, and the first event the client sees is the first thing the *model* does. `worker_unreachable` fires only when the first-event deadline expires | The UI cannot tell "no worker has this yet" from "the model is thinking" (Paperclip's queued / held / "Waiting to start" rule) |
| **Tool calls** | `tool_start` carries the raw tool name and raw args; `tool_end` carries only the name. No call id, duration, outcome or result summary | No sentence-style rail ("Searched the catalog · 6 results"), no duration, and parallel calls of the same tool can't be paired reliably |
| **Approvals** | `approval_required` sends `[{name, args}]`. The UI renders `⏸ Approve this action? create_ticket({"title":…})`. `ResumeRequest` is `{thread_id, approved}` | Shows tool ids and JSON to people (OpenWork C3 forbids exactly this). No tier, effect, target or reversibility. The decision isn't bound to the exact request that was displayed (Paperclip playbook §9: stale or duplicate answers) |
| **Capability tiers** | `ToolCapability = read_only / mutating / outward` exists in `app/agent/tools/builtin.py` and per-domain `TOOL_CAPABILITIES`, with undeclared tools treated as outward | Known server-side, never sent to clients |
| **Usage and cost** | The cost meter records each model call (`COST_GOVERNANCE.md`); `GET /usage` returns tenant totals, budgets and credits | No usage for each turn on the stream; the model and route that *actually* ran (`State.model_route`) aren't visible |
| **Domain metadata** | `AgentManifest{name, system_prompt, allowed_tools, suggest_followups, prefetch}` | No display name, description, icon or tool labels, so neither the UI nor a platform can present a domain as an "agent" |
| **Stricter-only request flags** | `require_approval`, `unattended`, `replay_key` on `ChatRequest` ("can only make a turn stricter or safer to repeat") | The right pattern. A per-turn `read_only` flag (Eigent's "Read only" mode) fits it exactly and is missing |
| **Errors** | One envelope with stable codes (`app/core/errors.py`) | No split between *blocked* (policy, tier, credit gate: neutral, with a lock) and *failed* (red) |
| **Web UI** | A single `index.html` (about 2,200 lines, vanilla JS) plus `auth.js`. It renders exactly the documented SSE vocabulary and resumes dropped streams | Activity log, approval box and error rendering need the redesign in Part B |
| **MCP** | Stdio and HTTP gateway servers expose read tools under the caller's verified identity, with no write tools behind the gateway (Principle II) | Already a clean integration path in the other direction (graph-agent-core as a *tool* for other agents) |

---

## 3. Rules for every change in this plan

1. **Additive only.** New event types and new optional fields. No renames, no removals. Clients
   that ignore unknown `type`s keep working. Each change raises `info.version` (minor:
   3.4 → 3.5 …) and passes `make openapi-breaking`.
2. **The server owns presentation facts.** Labels, tier, effect, reversibility and outcome are
   computed in the runtime from tool metadata. Clients never parse tool args to guess risk.
3. **Bounded and scrubbed.** Every new free-text field (labels, summaries, effects) has a length cap
   and goes through the existing scrubbing (`app/core/scrubbing.py`) before it reaches a stream.
   Untrusted model output never becomes a label without that.
4. **Unknown is not zero.** A cost the runtime can't determine is `null`, never `0` (Paperclip
   playbook §8: "absent usage is not zero cost").
5. **Constitution first.** Nothing here adds an auto-approve, an "allow for session" or a "full
   access" mode for `mutating` or `outward` tools. New request flags may only make a turn stricter.
6. **One vocabulary for all channels.** The web UI, Telegram (`telegram/approvals.py`), the CLI and
   external platforms read the same events, and the committed OpenAPI document stays the single
   description of them.

---

## 4. Part A: the runtime contract (backend)

Each item lists what to add, why (with the research reference), and where it lives.

### A1. A `status` event: queued, started, waiting

```jsonc
{ "type": "status", "state": "queued",   "at": "2026-11-22T10:00:00Z" }                          // API, right after publish
{ "type": "status", "state": "started",  "at": "…", "worker": "w-3f2a" }                        // worker, on claim, before the graph runs
{ "type": "status", "state": "waiting",  "at": "…", "reason": "approval" }                      // optional; approval_required stays terminal
```

- **Why.** This lets the UI show Paperclip's sequence: "Working" from submit, then "Waiting to start"
  (no spinner) once roughly 30 s pass without `started`. It also tells a platform that the job was
  admitted, which is Multica's claim step and Paperclip's admission step.
- **Where.** The API writes `queued` into the results stream when it publishes; the worker writes
  `started` when it claims (`agent_worker.py`). The first-event deadline in
  `adapters/outbound/redis/queue.py` should then measure "no `started`" rather than "no event at
  all", which makes `worker_unreachable` more precise.
- **Optional later.** `{"state":"queued","position":2}` if queue depth is cheap to read from the
  consumer group.

### A2. Tool calls get an identity, a label, a tier and an outcome

```jsonc
{ "type": "tool_start", "tool": "create_ticket", "args": {…},           // existing fields kept
  "call_id": "call_8f…", "label": "Creating a support ticket", "tier": "mutating",
  "subagent": null, "at": "…" }
{ "type": "tool_end", "tool": "create_ticket",                          // existing fields kept
  "call_id": "call_8f…", "outcome": "ok", "duration_ms": 412,
  "summary": "Ticket #4821 created" }
```

- `call_id` is the LangGraph tool call id, the same one `idempotent()` keys on. It pairs start
  with end, even when parallel calls use the same tool.
- `label` and `summary` come from **tool metadata**: a short `display` entry next to each tool's
  `TOOL_CAPABILITIES` declaration, e.g. `"create_ticket": ("mutating", "Create a support ticket")`.
  An optional `summarize(result) -> str` per tool produces "6 results" or "Ticket #4821 created".
  With no summarizer there's no summary, which is fine.
- `outcome`: `ok | error | refused` (refused covers a policy or ctx refusal, `NO_CTX_REFUSAL` and
  allowlist denials). This feeds OpenWork T2: a step the agent recovers from stays neutral.
- **Why.** The sentence-style rail (OpenWork T1 and C4), durations, collapsing to "Worked for 8 s ·
  3 steps", and a canonical tool event for platforms (Paperclip playbook §8: "preserve stable …
  tool identities").

### A3. Approvals that name action, data and risk, bound to what was shown

```jsonc
{ "type": "approval_required",
  "tool_calls": [ { "name": "create_ticket", "args": {…} } ],            // existing field kept
  "approval_id": "apr_2c…",                                               // new: identity + revision of this pause
  "items": [ {                                                            // new: one per tool call, same order
      "call_id": "call_8f…",
      "tier": "mutating",
      "title": "Create a support ticket",
      "effect": "Opens ticket \"VPN down in Hanoi office\" at priority High",
      "target": "Support queue · tenant acme",
      "visible_to": "Support team",
      "reversible": true } ] }
```

and on resume:

```jsonc
POST /chat/resume  { "thread_id": "…", "approved": true, "approval_id": "apr_2c…" }   // approval_id optional for old clients
```

- `title`, `effect`, `target`, `visible_to` and `reversible` come from a per-tool `describe(args)`
  hook next to the tool, with a safe fallback: the title from the display label, the effect as
  "Runs with the details below", and `reversible: false`. The fallback errs toward caution.
- **`approval_id`** is minted when the pause is checkpointed and stored with it. A resume that
  names a different or stale `approval_id` is refused with a new error code
  (`approval_superseded`). This stops a double click, an old tab or a replayed Telegram button from
  approving something other than what the person saw. It serves Principles II and IV.
- `GET /chat/sessions/{id}/pending_approval` returns the same `approval_id` and `items`, so a
  reloaded page or a platform rebuilds the same card.
- **No "Edit" yet.** Editing args would need a new approval revision and a way to amend the
  checkpoint. Keep the card to Approve / Decline until that exists, and don't show a control the
  runtime can't honour.

### A4. A `usage` event before `done`

```jsonc
{ "type": "usage", "model": "qwen2.5:14b", "route": "local",          // the model and tier that actually ran
  "input_tokens": 1830, "output_tokens": 212, "cost_usd": 0.0021,     // cost_usd null when unknown
  "model_calls": 2 }
```

- The source is the per-turn cost meter (`app/agent/cost/`) plus `State.model_route`. It includes
  subagent calls, and says so in the field doc.
- **Why.** Cost on each turn (Multica's execution log, Javis's engine badge). It's also the
  `usage` / `costUsd` / `model` / `provider` that platform adapters return (Paperclip's
  `AdapterExecutionResult`).

### A5. Pending approvals across sessions

`GET /chat/approvals` returns the caller's pending approvals in the current domain (or all domains
with `?domain=*`): `[{thread_id, session_title, approval_id, items, requested_at}]`. It's
tenant-scoped and principal-scoped like every session read (Principle I).

- **Why.** This is the "Needs you" list (Paperclip Inbox, Multica Inbox, Javis "Needs you"). Today a
  paused turn is only discoverable by opening its session.

### A6. A runtime card (discovery)

`GET /v1/runtime` (or `/.well-known/agent-runtime.json`) returns a static description:

```jsonc
{
  "runtime": "graph-agent-core", "version": "3.5.0", "api": "/openapi.json",
  "agents": [ {                                   // one per domain this deployment serves
      "id": "support", "display_name": "Support desk", "description": "Answers from the help centre and opens tickets.",
      "icon": "lifebuoy",
      "tools": [ { "name": "create_ticket", "label": "Create a support ticket", "tier": "mutating" }, … ] } ],
  "capabilities": {
      "streaming": "sse", "resume_stream": true, "sessions": true,
      "approvals": { "required_for": ["mutating", "outward"], "modes": ["read_only", "require_approval"], "auto_approve": false },
      "cancel": true, "steer": false, "queued_followups": false,
      "images": true, "citations": true, "usage": true, "idempotency": ["replay_key"] },
  "auth": { "modes": ["identity", "headers"], "door": true }
}
```

- `AgentManifest` gains `display_name`, `description` and `icon`, with defaults so existing domains
  keep working.
- **Why.** Paperclip exposes adapter *capability flags* at `GET /api/adapters`, and its UI switches
  features on from them. A platform should never hardcode what graph-agent-core supports. Report
  `steer: false` and `queued_followups: false` honestly (Paperclip playbook §10.2: "Advertise
  unsupported controls honestly").

### A7. A stricter-only `read_only` flag on `ChatRequest`

`read_only: true` means only `read_only`-tier tools are bound for this turn. Mutating and outward
tools are not offered to the model at all, not merely gated. Like `require_approval`, it can only
make a turn stricter, so Principle II is untouched.

- **Why.** Eigent's "Read only" mode, Javis's "Read only" level, and a safe default for platforms
  that run agents unattended. It's also a cleaner answer than `unattended` when no approval UI
  exists.

### A8. Blocked is not failed

Group error codes into families in the envelope: `details.kind = "blocked" | "failed" | "lost"`.
`blocked` covers policy, tier, credit gate, budget cap and ctx refusal; `lost` covers
`checkpoint_lost`, `checkpoint_incompatible` and `worker_unreachable`. The same `kind` goes on
`tool_end.outcome = "refused"` events.

- **Why.** OpenWork C5 and P4: blocked uses neutral ink and a lock and says who can change it; red
  is only for failure. Budget caps from `COST_GOVERNANCE.md` are the main case.

### A9 (later). A non-streaming run API for webhook-style platforms

`POST /v1/runs {agent, thread_id, message, replay_key, callback_url?}` returns `{run_id}`;
`GET /v1/runs/{id}` returns the status, the final text, citations, usage and any pending approval.
`callback_url` receives one signed POST on a terminal state.

- **Why.** Paperclip's generic `http` adapter is fire-and-forget plus a callback, and some
  platforms prefer that to holding an SSE connection. Build it only when a real consumer needs it.
  The SSE contract covers everything else.

---

## 5. Part B: the web UI (`index.html`)

**Update 2026-11-26:** decision 1 below was taken the other way. The page is being rewritten as a React
application in `web/` ([specs/023](../specs/023-web-client/plan.md)), so B1–B11 are built there, and the
following paragraph's no-build advice and ES-module split no longer apply.

Keep it framework-free for now. It's a reference client, and a no-build page is easy to audit. Do
split it into a few native ES modules (`stream.js`, `turn.js`, `approval.js`, `sidebar.js`) as part
of B1, because the file is already about 2,200 lines (CLAUDE.md's "~500 lines is a prompt").

Each item lists what to build, the research rule behind it, the backend item it needs, and when
it's done.

| # | Change | Research basis | Needs | Done when |
|---|---|---|---|---|
| **B1** | **Approval card** above the composer, replacing the inline `⏸ Approve this action? name(JSON)` line. Shows title, a one-line effect, target, who sees it, whether it's reversible, and a tier chip. **Approve** (`⏎`) / **Decline** (`esc`). Raw args sit behind "Technical details". The composer stays usable. Several calls appear as one card with a row per call. A Decline that ends the turn says so | OpenWork P9, T4, C3; Paperclip "card above composer" | A3 | No tool id or JSON is visible by default; a resume sends `approval_id`; a stale approval shows "This request was replaced" with no second action |
| **B2** | **Activity rail**: one row per `call_id` with an icon by tier, the sentence label, a summary and a duration on the right. Only the running row animates. When the turn finishes it collapses to "Worked for 8 s · 3 steps" (click to expand). Subagent calls are indented under "Asked the research subagent". `refused` rows are neutral with a lock | OpenWork T1, T2, C4, V6; Paperclip shimmer rule | A2 | No raw tool names in rows; parallel calls pair correctly; reduced motion turns animation off |
| **B3** | **Honest turn status.** "Working" with a subtle shimmer from submit. If `status:started` hasn't arrived after 30 s, show "Waiting to start" with no spinner, plus queue position when sent. `retry` shows a quiet "Rewriting the answer…" in place, with no layout jump | Paperclip queued / held rules; OpenWork P11 | A1 | A stopped worker gives "Waiting to start" and then the `worker_unreachable` message, never an endless spinner |
| **B4** | **Turn footer**: `model · route · 2.0k tokens · $0.002` in muted mono under each answer, with a session total in the header. `null` cost shows as "cost unknown" | Multica execution log; Javis engine badge; Paperclip "machine values look machine-made" | A4 | Values match `/usage` deltas; unknown cost isn't shown as $0 |
| **B5** | **"Needs you"** entry in the sidebar with a count badge, listing pending approvals across sessions. Each row opens its session with the card ready. Use a browser notification only when the tab is hidden, and only for approvals | Paperclip Inbox; Multica "Needs attention"; Javis "Needs you" | A5 | A paused turn from yesterday is one click away; no notification for state already on screen |
| **B6** | **Blocked vs failed.** `kind: blocked` renders neutral with a lock icon, the reason and who can change it ("Monthly budget reached · ask a workspace admin"). Only `failed` uses the danger colour | OpenWork C5, P4; Paperclip "expected cancellation is grey" | A8 | No red for a budget cap or policy refusal |
| **B7** | **Domains as workspaces.** The domain picker shows a colour dot, display name and description (from A6). Sessions are grouped by domain in the sidebar. The suggested-questions empty state uses the domain's description as a one-line value plus one action | Eigent Space; OpenWork workspace; OpenWork feature intros I3–I5 | A6 | No hardcoded domain names in `index.html` |
| **B8** | **Composer pills**: domain · **Read only** toggle · **Ask before every step** toggle (`require_approval`) · round send/stop button. Shortcuts: `⏎` send, `⇧⏎` newline, `esc` stop | OpenWork T5; Eigent approval-mode selector (stricter-only subset) | A7 | Toggles only add strictness; their state is visible on each turn |
| **B9** | **Copy pass**: sentence case; remove descriptive subtitles; verb-first buttons ("Approve", "Decline", "Upload documents"); one term per concept (pick *session* or *conversation*, not both); errors say what happened and what to do | Multica "UI descriptions"; OpenWork P1, P2, C1, C6, C7 | none | Every visible string checked against the copy rules in B11 |
| **B10** | **States**: a skeleton for the session list and history (not "Loading…" text), designed empty states, an offline banner when a stream drops ("Reconnecting… · picks up where it left off", using the existing `Last-Event-ID` re-read), and "Couldn't load · Retry" inline | OpenWork "States"; Paperclip offline recovery | none | Each data-bearing area has loading, empty, error and offline states |
| **B11** | **`static/DESIGN.md` plus a guard test.** One page: tokens (the CSS variables already in `index.html`), the status vocabulary (queued / working / waiting / blocked / failed / done) mapped to colours, copy rules, the states rule, and "render only the documented event vocabulary". A small test fails on raw hex outside the token block and on tool names rendered without a label | Paperclip principles 2, 5, 7; Eigent §14; OpenWork checklist | none | `tests/adapters/inbound/api/test_web_ui_design_rules.py` passes; later UI PRs cite DESIGN.md rules |

**Not in this plan, on purpose:** a mascot, a knowledge-graph canvas, a run timeline modal, a task
board, an org chart. Those belong in the platform (Part D). graph-agent-core's UI should stay small
enough to serve as the conformance reference for the contract.

---

## 6. Part C: integration readiness

### C1. What any platform needs from a runtime

Combining Paperclip's adapter contract (`AdapterExecutionContext` / `AdapterExecutionResult`,
capability flags, `testEnvironment`), its runner integration playbook (§8 events, §9 durable human
interactions, §10 continuity and controls) and Multica's daemon flow (claim, heartbeat, stream
back), a platform needs ten things from a runtime. Here is graph-agent-core against each, before
and after Part A:

| # | Platform need | graph-agent-core today | After Part A |
|---|---|---|---|
| 1 | **Start a turn** with an id the platform can retry safely | `POST /chat/stream/queued` + `replay_key` + submit dedup | Same, plus A9 for webhook platforms |
| 2 | **Session continuity** (resume the same conversation next time) | `thread_id`, owned per principal; the checkpointer | Same; the platform stores `thread_id` as its `sessionParams` |
| 3 | **Canonical event stream** with stable identities | Closed SSE vocabulary, `id:` positions, `Last-Event-ID` re-read | Plus `status`, `call_id`, outcome, `usage` (A1, A2, A4) |
| 4 | **Durable human interactions** (persist → render → decide → deliver once) | Pause checkpointed; `pending_approval`; `resume` | Plus `approval_id` binding and a cross-session list (A3, A5) |
| 5 | **Cancel / stop** | `POST /chat/cancel` (streaming or paused) | Same |
| 6 | **Steer / queued follow-ups** | Not supported | Reported as `false` in the runtime card (A6) |
| 7 | **Usage and cost** for each run | Meter records it; tenant totals only | `usage` event per turn (A4) |
| 8 | **Health / environment test** | `/health`, `/health/ready` with checks and warnings | Plus the runtime card for config validation (A6) |
| 9 | **Capability discovery** | None | Runtime card (A6) |
| 10 | **Identity on behalf of a person** | The door's signed identity (`AUTH_MODE=identity`); tenant and principal never come from tool args | Same, plus a documented *service principal* for platform-initiated runs (C3) |

### C2. Three ways a platform can integrate

1. **HTTP + SSE adapter (recommended first).** The platform runs a thin adapter that calls
   `/chat/stream/queued`, maps events to its transcript, turns `approval_required` into its own
   inbox item and calls `/chat/resume`. This is the natural fit because graph-agent-core is a
   service, not a local CLI: Paperclip's `hermes_gateway` exists for the same reason ("for an
   already-running … API server; it does not start the local CLI").
2. **MCP (already built, the other direction).** Other agents (Claude Code, Codex, a platform's
   agents) call graph-agent-core's read tools through the MCP gateway under the caller's verified
   OAuth identity. Keep it read-only (Principle II). Use it for "let my coding agent query the
   knowledge base", not for running whole turns.
3. **A standard agent protocol (later).** Many runtimes now speak a stdio or HTTP agent protocol
   (Multica drives a long list of CLIs over ACP; agent-to-agent protocols exist for remote agents).
   If your platform settles on one, write a **translation adapter** outside the core, mapping that
   protocol to the Part A contract. Don't put a protocol into `app/` before a consumer needs it, and
   check the current spec version when you do. The ports-and-adapters layout already has the right
   home for it: a new driving adapter under `app/adapters/inbound/`, calling only
   `app.agent.runtime`.

### C3. Identity for platform-initiated runs

A platform acts in two ways: *on behalf of a person* (the person directing the work, Paperclip's
"responsible user") or *as itself* (a routine at 9 AM). Map both onto what exists:

- **On behalf of a person.** The platform sends the person's own token through the door, and
  graph-agent-core sees that principal. This is the default.
- **As itself.** A dedicated service principal per platform installation, created in Casdoor,
  rate-limited by authlimit like anyone else, with its own budget caps. Combined with `read_only`
  (A7) or `unattended`, it never reaches a mutating tool without a human approving it through the
  platform's inbox.
- Never add a "tenant" or "on behalf of" field the caller can set. That would break Principle I.

### C4. Sketch: a platform adapter for graph-agent-core

Pseudocode, in the shape of Paperclip's `execute()` contract; a Python platform would look the same:

```ts
async function execute(ctx: AdapterExecutionContext): Promise<AdapterExecutionResult> {
  const threadId = ctx.runtime.sessionParams?.threadId ?? crypto.randomUUID();
  const res = await fetch(`${base}/chat/stream/queued`, {
    method: "POST",
    headers: { Authorization: `Bearer ${tokenFor(ctx)}`, "X-Domain": ctx.config.domain },
    body: JSON.stringify({ message: renderTask(ctx.context), thread_id: threadId,
                           replay_key: ctx.runId, read_only: ctx.config.readOnly ?? false }),
  });
  let usage, outcome = "done";
  for await (const ev of sse(res, { resumeFrom: res.headers.get("X-Request-Id") })) {
    switch (ev.type) {
      case "status":            await ctx.onMeta?.({ phase: ev.state }); break;
      case "tool_start":        await ctx.onLog("stdout", JSON.stringify({ kind: "tool", ...ev })); break;
      case "tool_end":          await ctx.onLog("stdout", JSON.stringify({ kind: "tool_done", ...ev })); break;
      case "token":             await ctx.onLog("stdout", ev.content); break;
      case "usage":             usage = ev; break;
      case "approval_required": await persistInteraction(ctx, threadId, ev); outcome = "waiting"; break; // platform inbox → later /chat/resume with approval_id
      case "error":             return fail(ev, threadId);
    }
  }
  return { exitCode: 0, signal: null, timedOut: false,
           sessionParams: { threadId }, model: usage?.model, provider: "graph-agent-core",
           usage: usage && { inputTokens: usage.input_tokens, outputTokens: usage.output_tokens },
           costUsd: usage?.cost_usd ?? null };
}
// testEnvironment(): GET /health/ready + GET /v1/runtime → pass/warn/fail checks
```

### C5. A conformance suite as the integration guarantee

Add `tests/contract/`: a recorded set of turns (plain answer, a tool call, parallel tools, approval
→ approve, approval → decline, stale approval, cancel mid-stream, dropped connection → re-read,
`worker_unreachable`, budget blocked). Each one asserts the exact event sequence and fields. The
web UI, the Telegram bot and any platform adapter are built against those recordings. The
event-model tests in `tests/agent/conftest.py` already hold the real events to the OpenAPI
document; this suite holds the *sequences*.

---

## 7. Part D: blueprint for the new platform

You'll build a platform that hosts graph-agent-core *and* other runtimes. From the research, this
is what it should own, and how graph-agent-core plugs into it.

### D1. The platform's own objects

| Object | What it is | From |
|---|---|---|
| **Workspace** | Tenant boundary: people, agents, connections, budgets | Multica workspace, Paperclip company |
| **Agent** | A named teammate: role, avatar, owner, **runtime binding** (adapter + config), permissions | All three work-management products |
| **Runtime** | Something that can run turns: graph-agent-core, Claude Code, Codex, OpenCode… with its capability card | Paperclip adapters, Multica runtimes |
| **Task / Thread** | The unit of work; holds the conversation, plan, sub-tasks, artifacts, properties | Paperclip task, Multica issue, Eigent session |
| **Run** | One execution attempt of an agent on a task: status, events, usage, timeline | Multica execution log, Paperclip heartbeat run |
| **Interaction** | A durable human decision: approval, question, plan review. Persisted before shown, delivered once | Paperclip playbook §9 |
| **Artifact** | A file or document a run produced, previewable and commentable | Paperclip artifacts, OpenWork side panel |
| **Usage record** | Tokens and cost for each run, attributed to agent, task, person and workspace | Multica, Paperclip budgets |

### D2. One canonical event vocabulary across runtimes

The platform normalises every runtime into one set. graph-agent-core (after Part A) maps onto it
almost one-to-one, which is the reason to do Part A first:

| Platform event | graph-agent-core event | Claude Code / Codex-style CLI (via adapter) |
|---|---|---|
| `run.status` | `status` | process spawned / first output |
| `message.delta` | `token` | streamed text |
| `message.reset` | `retry` | rarely |
| `tool.started` / `tool.finished` | `tool_start` / `tool_end` (with `call_id`) | tool-use blocks |
| `interaction.requested` | `approval_required` (with `approval_id`, `items`) | permission prompts |
| `context.compacted` | `compacted` | compaction notices |
| `sources` | `citations` | n/a |
| `suggestions` | `followups` | n/a |
| `usage` | `usage` | usage receipts |
| `run.failed` / `run.blocked` | `error` with `details.kind` | exit codes / stderr |
| `run.finished` | `done` | exit 0 |

### D3. The platform UI, built from the research

- **Needs you** as the home screen: approvals, questions and blocked runs, across every runtime,
  with counts (§5.4 of the research).
- **Task thread**: the conversation plus a collapsible activity rail, the approval card above the
  composer, and a properties pane (status, assignee agent, runtime, budget, reviewers).
- **Run view**: a model-time lane and a tool-time lane, searchable steps, input/result detail, cost
  (Multica).
- **Agents page**: "Working now", recent work with success rate, runtime health (Multica Team).
- **Composer**: model / effort / mode pills, round send/stop, queued messages; *steer* only for
  runtimes whose card says they support it.
- **Design contract from day one**: a DESIGN.md written for coding agents, token-only styling
  enforced by lint, screenshots required in PRs (research §5.8). Start from OpenWork's rule set;
  borrow Paperclip's state-presentation rules and Eigent's accessibility contract.

### D4. What the platform must not assume about a runtime

From Paperclip's playbook, worth repeating because it's where integrations fail: don't fake
steering by sending a concurrent prompt; don't mark an approval delivered because a local callback
resolved; don't treat missing usage as zero; don't replay an old permission into a new session.
graph-agent-core's `approval_id`, `replay_key`, `null` cost and honest capability flags exist so
the platform never has to guess.

---

## 8. Phasing and PR plan

One logical change per PR, about 400 hand-written lines each (CLAUDE.md). Contract PRs regenerate
`openapi.json`, update the Bruno collection and raise `info.version`. Parts A and C are a feature,
so they should start with `/speckit-specify` as **specs/024**; Part B can follow that spec's task
phases.

| Phase | PR | Contents | Depends on |
|---|---|---|---|
| **1 · Contract** | PR1 | A1 `status` event (API `queued`, worker `started`, deadline keyed on `started`) | none |
| | PR2 | A2 tool `call_id`, `label`, `tier`, `outcome`, `duration_ms`, `summary` + tool display metadata in each domain | none |
| | PR3 | A3 approval `items` + `approval_id`, `approval_superseded`, `pending_approval` parity; Telegram buttons send `approval_id` | PR2 (labels) |
| | PR4 | A4 `usage` event + A8 error `kind` | none |
| **2 · Web UI** | PR5 | B11 `static/DESIGN.md` + guard test, ES-module split (no behaviour change) | none |
| | PR6 | B2 activity rail + B3 honest status | PR1, PR2, PR5 |
| | PR7 | B1 approval card | PR3, PR5 |
| | PR8 | B4 turn footer + B6 blocked vs failed + B9 copy pass + B10 states | PR4, PR5 |
| **3 · Discovery** | PR9 | A6 runtime card + `AgentManifest` display fields + A7 `read_only` flag | PR2 |
| | PR10 | A5 `/chat/approvals` + B5 "Needs you" + B7 domains as workspaces + B8 composer pills | PR3, PR9 |
| **4 · Integration** | PR11 | C5 contract suite (recorded sequences) + `INTEGRATION.md` (C1–C4, identity guide) | Phase 1 |
| | PR12 (when needed) | A9 run API + signed callbacks; first real platform adapter lives in the *platform* repo | PR11 |

Phase 1 is useful on its own: Telegram and the CLI get better messages from it straight away.
Phases 2 and 3 can run in parallel once Phase 1 merges.

---

## 9. Constitution, docs and tests

**Principles touched.**
- **I (tenant isolation).** `/chat/approvals` and the runtime card are scoped to the caller. The
  service principal (C3) is an ordinary principal, so nothing takes a tenant from input.
- **II (mandatory approval).** No auto-approve mode is added. `read_only` and `require_approval`
  only add strictness, and `approval_id` makes each approval specific to what was displayed.
- **IV (exactly-once).** `approval_id` makes a resume single-use for a given pause. `call_id` on
  events is the same id `idempotent()` already dedups on. `replay_key` covers platform retries.

**Docs to update** (in the PR that changes the behaviour):
- `openapi.json` (`make openapi`), `bruno/` requests, a version raise (`make openapi-version`).
- `GRAPH_PATTERNS.md`: new patterns for the status event, approval binding and the usage event,
  each with the failure it prevents.
- `ARCHITECTURE_DIAGRAMS.md`: the turn and approval sequence diagrams gain `status`, `usage` and
  `approval_id`.
- `README.md`: the event vocabulary table; a pointer to `INTEGRATION.md`.
- `.env.example`: any new setting (for example `TURN_WAITING_TO_START_SECONDS` if the 30 s
  threshold becomes configurable server-side).

**Tests.**
- The event-model tests hold every new event to the committed document (existing mechanism).
- Regression tests: a stale `approval_id` is refused; parallel same-tool calls pair by `call_id`;
  `cost_usd` is `null` (not 0) when the meter has no price; `read_only` never binds a mutating
  tool; `worker_unreachable` keys on the missing `started` event.
- `tests/contract/` sequences (C5); the web UI design guard (B11).

---

## 10. Decisions for you

1. **Where the product UI lives.** This plan keeps `index.html` as a small reference client and puts
   the rich UI in the new platform. If you'd rather make graph-agent-core's own UI the product, B1–B11
   still apply, but you'd also want a framework and a build step.
   **Decided 2026-11-26:** a framework and a build step ([specs/023](../specs/023-web-client/spec.md)).
2. **Approval granularity.** Today a resume approves or declines all pending calls together. Should
   the card allow approving some calls and declining others? That's a larger change to the resume
   path. The plan assumes all-or-nothing for now.
3. **Discovery URL.** `GET /v1/runtime` (versioned, simple) or `/.well-known/agent-runtime.json`
   (conventional for discovery). Either works; pick one before PR9.
4. **First integration target.** Your own platform first (recommended: you control both sides of
   the contract), or a Paperclip/Multica adapter as an external proof?
5. **Spec number.** Parts A and C as **specs/024** (023 is the web client) via `/speckit-specify`, with Part B in that spec's
   task phases, or Part B as its own spec?
