# Web client

The browser UI of Graph Agent Core: React, TypeScript, Vite, Tailwind, TanStack Query (server state), Zustand (client state), oxlint and
Vitest. It talks only to the HTTP API in [`openapi.json`](../openapi.json), and renders only the documented stream events.

It has **its own image and container** ([Dockerfile](Dockerfile), the `web` service; RUNTIME_PATTERNS.md 116). `make up-app` serves it at
http://localhost:8085; behind the front door (`make up-all`, production) it is https://api.localhost:8443/ or the API's domain.

`GET /` is the built `index.html`, with the sign-in mode written into its `<meta name="agent-auth">` tag, and the bundle is under
`/static/assets/` (public at the front door). In the container Caddy writes the tag from `AUTH_MODE` and `WEB_AUTH_*`
([server/agent-auth.tmpl](server/agent-auth.tmpl)); on the host the API does both (`app/adapters/inbound/api/web_auth.py`): `make web-build`
before `make serve`. Change one writer and the other with it: [server/testdata/agent-auth.json](server/testdata/agent-auth.json) holds both
to one answer (`make web-image-check`, and `tests/core/test_web_container.py`).

## Run it

```bash
make web-install        # npm ci, once (Node 22, see .nvmrc)
make serve              # the API on :8000, with AUTH_MODE=headers (the .env.example default); and a worker: make agent-worker
make web-dev            # http://localhost:5173, hot reload; proxies /chat, /ingest, /health and /usage to the API
```

`WEB_API_TARGET=http://host:port make web-dev` points at another API. An API in `identity` mode answers the dev server with 401: sign-in
redirects to the front door's own address, so it can only be tried there.

`make web-check` runs what CI runs: the generated API types are fresh, lint, type check, tests, build. `make web-image-check` builds the
image and checks what it serves.

## Layout

```
src/
  main.tsx            the entry: theme, wiring, sign-in return, render
  app/                composition: layout, the query client, and wiring.ts, the one place that joins features
  features/
    auth/             sign-in (authorization code with PKCE) and the Authorization header
    chat/             the live conversation: model/ (messages, event reducer, turn store), ui/
    sessions/         the sidebar's conversation list (grouped by last activity), history pages, restoring a pause
    theme/            light and dark
    uploads/          documents for the knowledge base, with per-file progress
    workspace/        tenant, principal, domain, and each domain's text
  shared/             api/ (the client, the stream parser, the contract's types), lib/
  test/               test setup, a fake API, the architecture test
```

### Rules (tests, not conventions: `src/test/architecture.test.ts`)

- `shared` knows no feature. A feature uses `shared` and its own files, and another feature only through its `index.ts`, only along an
  edge listed in the test (today: `sessions` uses `chat` and `workspace`). Anything else is joined in `app/wiring.ts`.
- Only `shared/api/http.ts` calls the app's API, so identity headers and the 401 rule cannot be skipped. The one other `fetch` is
  sign-in's code exchange with the authorization server.

### Conventions

- Server state (anything the API can be asked again) lives in TanStack Query; what only this tab knows lives in a Zustand store.
- Model output is untrusted: no raw HTML, and nothing is fetched without a click (`features/chat/ui/Markdown.tsx` says why).
- An answer is markdown from its first token. While it streams, `remend` closes what is half written (a link being typed is plain text)
  and each new word fades in; both sit inside `Markdown.tsx`, behind the same two rules. The committed answer is the model's text unchanged.
- A fenced block that names its language is highlighted (`rehype-highlight`, colours in `app/styles.css`). A ```` ```markdown ```` block shows
  what it will look like, with its source one click away: a model asked for "a Markdown document" fences it.
  Not done: bold around an unfinished `code` span shows its asterisks until the span closes; each token re-parses its whole message (smooth on
  the 2 KB answers checked, not measured on very long ones); a ```` ```markdown ```` block that itself holds a fenced block ends at the inner
  fence, as Markdown defines it; the highlighter's 37 languages are in the one bundle (about 200 KB gzipped in all, past Vite's size warning).
- The transcript follows new text only while the reader is at the end (`Transcript.tsx`).
- Colours are the tokens in `app/styles.css` (`bg-elevated`, `text-muted`, ...), never raw values, so both themes have one source.

### The API's types

`src/shared/api/schema.gen.ts` is generated from `../openapi.json`: after `make openapi`, run `make web-api` (CI fails while it is stale, a
changed route description included, since the file carries them).
`contract.test.ts` fails when a stream gains an event type the client does not handle.

### Names the browser tests use

`tests/live/` and `scripts/checks/authlimit_web_check.py` find things by id (`#input`, `#send`, `#messages`, `#authBox`, ...) and by class
(`msg`, `user`, `assistant`, `status`, `tool-activity-line`, `answer-text`, `draft-text`, `approval`, `approval-question`, `citations`,
`citation-item`). Keep them when restyling.

## Adding a feature

A folder under `features/` with an `index.ts` as its public face; its API calls in its own `api.ts`, using `shared/api`; compose it in
`app/App.tsx`. If it needs another feature, prefer joining them in `app/wiring.ts` before adding an edge to the architecture test.
