# Feature Specification: The web UI as a React application

**Feature Branch**: `feat/web-app` (phase 1), `feat/web-app-cutover` (phase 2)

**Created**: 2026-11-26

**Status**: Implemented in two PRs: the application (phase 1), then serving it and deleting the old page (phase 2). Sign-in against a real Casdoor is not yet run (plan.md).

**Input**: "a new PR to use a react app project with oxlint, tanstack query, zustand, tailwind, vite...etc. modular architecture...as a replace to this index.html file". The maintainer then chose two PRs over one or four.

## Why

1. **The page outgrew one file.** `static/index.html` is about 2,000 lines of markup, style and script sharing mutable globals, plus `static/auth.js`. Nothing in it can be type-checked or unit-tested, and the turn logic can only be exercised through a browser and a live model.
2. **Bugs lived in the gaps.** Reading it for this rewrite found several (listed in plan.md), each in state shared between handlers.
3. **The product direction needs components.** `docs/AGENT_PLATFORM_ALIGNMENT_PLAN.md` Part B (approval card, activity rail, honest status) is hard to build on the single file. This settles its decision 1.

## User Scenarios & Testing

### User Story 1 - Everything the page did, the application does (P1)
**Acceptance**: streaming answers with tool activity, retry, compaction and system notes; approve, reject and stop, with a pause restored after a reload; clarification options, citations and follow-ups; the conversation list with paged history; domains with their text and example questions; tenant and principal switching; sign-in in identity mode; document upload with per-file progress; attaching an image; light and dark.

### User Story 2 - A change in the API cannot be silently ignored (P1)
**Acceptance**: the request and event types are generated from `openapi.json`; CI fails when they are stale and when a stream publishes an event the client does not handle.

### User Story 3 - A feature can be changed without reading the others (P2)
**Acceptance**: features are separate modules; a test fails the build on an import that crosses them outside the allowed edges.

### User Story 4 - Cutover loses nothing (P1, phase 2)
**Acceptance**: `GET /` serves the built application with the sign-in mode written into it; the image builds it; the browser tests and the real sign-in check pass against it; the old page and script are gone.

## Requirements

- **FR-001** React, TypeScript (strict), Vite, Tailwind, TanStack Query for server state, Zustand for client state, oxlint with type-aware rules, Vitest.
- **FR-002** The security properties of the old page hold: no third-party script at run time, no raw HTML from model output, no request made from model output without a click, the token only in `sessionStorage` and only sent to this origin, PKCE as before (same storage keys, so a signed-in tab stays signed in across the cutover).
- **FR-003** Built assets live under `/static/`, which the front door already treats as public, so neither Caddy nor authlimit changes.
- **FR-004** The element ids and class names the browser tests use are kept.
- **FR-005** Node is needed only to build, test and develop `web/`; the Python targets and tests do not need it.

## Success criteria
SC-001 CI's `web` job is green on Linux from the lock file. SC-002 The application, in a real browser against the real stack, completes a tool turn, an approval, a history replay and an upload. SC-003 `make lint`, `make typecheck` and the hermetic suite are green.
