# Implementation Plan: The web UI as a React application

**Spec**: [spec.md](spec.md) · **Branches**: `feat/web-app`, then `feat/web-app-cutover` · **Base**: `main`

## Design in six decisions

1. **Modules by feature, one composition root.** `web/src/features/*` do not import each other, except along edges a test lists (today `sessions` uses `chat` and `workspace`); `app/wiring.ts` joins the rest. Identity headers reach the API client through one bound function, the same shape as this repo's ports.
2. **Server state and client state are different things.** History, the conversation list and the upload request are TanStack Query. The live turn is a Zustand store around a pure reducer (`applyEvent`), so the whole event vocabulary is tested without a browser.
3. **The contract is generated, not copied.** `openapi-typescript` writes the types; a test compares the handled event types with the document's. `system_note` is sent by the stream but missing from the document, and adding it is "breaking" under this repo's gate (it needs API 4.0.0), so it is typed by hand and left for a major version.
4. **Markdown is `react-markdown`, replacing the hand-written renderer.** The old reason for writing one (no CDN, no unverifiable vendored copy) does not apply to a locked npm dependency. Raw HTML is not parsed and images become links.
5. **No retries.** A retried upload would index a document twice.
6. **Dependencies at least two weeks old.** The lock was resolved with `npm install --before=2026-11-12`; Dependabot has the same cooldown.

## Phases

**Phase 1 (this change).** The application, its tests, `make web-*`, CI's `web` job, constitution 1.5.0. Not served; the image does not contain it.

**Phase 2.** A Node build stage in the `Dockerfile` (on the builder's platform, digest-pinned); `GET /` renders the built `index.html` (503 with the command to run when unbuilt) and a mount serves `/static/assets/` (404, not a 500, when unbuilt); `static/index.html` and `static/auth.js` deleted; the server-side tests, the live browser tests, `authlimit_smoke.py` and `authlimit_web_check.py` moved to the new page; CI's `test-live` builds it and `docker-build` checks the image has it; pattern 29 and the docs that named the old files updated.

## Where it behaves differently from the old page (on purpose)

Each was found by reading the old script, and each has a test:

- Text streamed before an approval pause left the answer that followed styled as an unfinished draft for good.
- A second approval after a resume ended the turn on screen: Stop disappeared and Send came back while the buttons were still shown.
- Changing domain during a pause left Send disabled, and Stop would cancel the previous conversation.
- Events from a conversation that was left (a tenant change mid-stream) could end the next conversation's turn.
- A file the upload refused was followed as job `null` and reported "HTTP 404" instead of its reason.
- "Thinking" stayed on screen after a turn that ended without streaming text.
- A refused request showed "HTTP 429" instead of the server's message.

Markdown now follows CommonMark with GFM tables; single newlines still break the line.

## Constitution Check

| Principle | Touched | How it holds |
|---|---|---|
| **I. Fail-closed tenant isolation** (NN) | No | The client sends what the old page sent. In identity mode it drops the tenant and principal headers; the server decides identity either way. |
| **II. Mandatory approval** (NN) | Presentation only | The approval shows the same names and arguments. A decision is sent only from a click, once per pause (tested for a double click). No server change. |
| **IV. Exactly-once** (NN) | No | No write path changes; the client never retries a request. |
| V. Bounded, observable failure | Yes | A stream that ends without a terminal event releases the page; a partial answer is marked as interrupted. |
| VI. Untrusted content is data | Yes | `features/chat/ui/Markdown.test.tsx`: no live HTML, no image fetched, no `javascript:` or `data:` link. |
| VII. Test discipline | Yes | Vitest runs with a fake API and no network; the Python serving tests need no Node (a stand-in build). `tests/live/test_chat_ui.py` drives the served page against a real model in CI. |
| VIII. Why-first docs, honest gaps | Yes | `web/README.md`; the gaps below. |
| Web client (1.5.0) | New | Own lock file and CI job; types from `openapi.json`; the architecture test. |

## Complexity Tracking

| Added | Why it is needed | Simpler alternative rejected because |
|---|---|---|
| A TypeScript application with its own toolchain (a further language beside Python and Go) | The maintainer asked for it; the single file cannot be type-checked or unit-tested, and the planned UI work needs components. | Splitting the page into native ES modules (the alignment plan's suggestion) keeps it build-free but gives no types, no component tests and keeps the hand-written renderer. |
| A Node build stage in the app image (phase 2) | The image must contain the built files. | Committing `web/dist` puts generated code in review and lets it drift from its source. |

## Not verified, and not done

- Sign-in against a real Casdoor with the new application (`make authlimit-web-check`): it replaces the containers of a running stack, so it was not run. Unit tests cover the flow, and a browser run with a stand-in authorization server exercised it on the production bundle.
- A rolling replacement of several API replicas can ask an old replica for a new bundle file (pattern 29). Not observed; nothing is deployed.
- No resume of a dropped stream with `Last-Event-ID`: the old page had none either.
- Only Chromium was run. No Firefox or Safari run.
