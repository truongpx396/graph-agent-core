# Tasks: one pull request per step

Item codes (A1, B2, C5) are the sections of [AGENT_PLATFORM_ALIGNMENT_PLAN.md](../../docs/AGENT_PLATFORM_ALIGNMENT_PLAN.md).

## Phase 1: contract

- [x] **PR1** the 4.0.0 contract and A1 `status`
  - [x] event models, `approval_superseded`, version, accepted-break entries, web types
  - [x] `queued` with the job, `started` from the worker, deadline keyed on a worker's event
  - [x] tests, pattern, diagrams, README
- [x] **PR2 to PR4**, one pull request at the owner's request
  - [x] A2 tool `call_id`, `label`, `tier`, `outcome`, `duration_ms`, `summary`, with a `display.py` beside each domain's tools
  - [x] A3 approval `items` and `approval_id`, `approval_superseded`, `pending_approval` parity, Telegram and the CLI send the id
  - [x] A4 `usage` event and A8 error `kind`
  - [x] a stop pressed while a turn is queued now runs nothing (found building step 1)
  - [x] a reclaimed resume answers only the pause it first found (found building step 3)

## Phase 2: web UI, in `web/`

- [x] **PR5 to PR8**, one pull request at the owner's request
  - [x] B11 `web/DESIGN.md` and a vitest guard
  - [x] B2 activity rail and B3 honest status, with reconnect from `Last-Event-ID`
  - [x] B1 approval card; the page sends `approval_id`
  - [x] B4 turn footer, B6 blocked and failed, B9 copy, B10 states

## Phase 3: discovery

- [ ] **PR9** A6 `GET /v1/runtime`, manifest display fields, A7 `read_only` (after PR2)
- [ ] **PR10** A5 `GET /chat/approvals`, B5 "Needs you", B7 domains, B8 composer (after PR3, PR9)

## Phase 4: integration

- [ ] **PR11** C5 recorded sequences in `tests/contract/` and an `INTEGRATION.md` with C1 to C4 (after phase 1)
- [ ] **PR12** A9 run API with signed callbacks, only when a consumer needs it
