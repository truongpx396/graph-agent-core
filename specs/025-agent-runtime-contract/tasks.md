# Tasks: one pull request per step

Item codes (A1, B2, C5) are the sections of [AGENT_PLATFORM_ALIGNMENT_PLAN.md](../../docs/AGENT_PLATFORM_ALIGNMENT_PLAN.md).

## Phase 1: contract

- [x] **PR1** the 4.0.0 contract and A1 `status`
  - [x] event models, `approval_superseded`, version, accepted-break entries, web types
  - [x] `queued` with the job, `started` from the worker, deadline keyed on a worker's event
  - [x] tests, pattern, diagrams, README
- [ ] **PR2** A2 tool `call_id`, `label`, `tier`, `outcome`, `duration_ms`, `summary`, with display metadata beside each domain's tools
- [ ] **PR3** A3 approval `items` and `approval_id`, `approval_superseded`, `pending_approval` parity, Telegram buttons send the id (after PR2)
- [ ] **PR4** A4 `usage` event and A8 error `kind`

## Phase 2: web UI, in `web/`

- [ ] **PR5** B11 `web/DESIGN.md` and a vitest guard
- [ ] **PR6** B2 activity rail and B3 honest status (after PR1, PR2, PR5)
- [ ] **PR7** B1 approval card (after PR3, PR5)
- [ ] **PR8** B4 turn footer, B6 blocked and failed, B9 copy, B10 states (after PR4, PR5)

## Phase 3: discovery

- [ ] **PR9** A6 `GET /v1/runtime`, manifest display fields, A7 `read_only` (after PR2)
- [ ] **PR10** A5 `GET /chat/approvals`, B5 "Needs you", B7 domains, B8 composer (after PR3, PR9)

## Phase 4: integration

- [ ] **PR11** C5 recorded sequences in `tests/contract/` and an `INTEGRATION.md` with C1 to C4 (after phase 1)
- [ ] **PR12** A9 run API with signed callbacks, only when a consumer needs it
