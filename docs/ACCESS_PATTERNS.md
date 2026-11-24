# Access patterns: who is calling, and how they get in

The edge service (`services/authlimit/`, specs/018), the API's identity, the MCP gateway and the production routing. The rule throughout: the identity comes from something the caller could not have written. Numbers are shared with the other pattern documents ([index](README.md#pattern-numbers)).

| # | Lesson |
|---|---|
| 82 | The MCP servers over HTTP take identity from a verified token on every request |
| 84 | The front door checks the token once and signs a short-lived assertion |
| 85 | Revocation is a local copy refreshed on a short interval |
| 86 | The API builds its caller from the signed identity and nothing a client sends |
| 97 | A request with no token is charged to the network address Caddy resolved |
| 98 | Production runs the same front door as development, from one routing file |

### 82. The same two MCP servers over HTTP take the identity from a verified token on every request, and neither the audience nor the scope is the boundary alone
`app/adapters/inbound/mcp/gateway.py` (specs/017) serves `query_employees` and the ops tools over Streamable HTTP. Same rule as TOOL_PATTERNS.md 78 with a new source: the identity is the token's `owner` (tenant) and `sub` (principal), never an argument or the launcher's environment.
- **A resource server and nothing else.** It issues nothing; it checks signature against the issuer's JWKS with an allow-listed algorithm (never the token's own header), `iss`, `exp`, `sub`, and an `aud` containing THIS endpoint's URL. The verifier never raises.
- **Both checks are needed:** Casdoor mints a token with any `resource` and any scope the application defines, so audience alone and scope alone each authorize wrongly. `/ecorp/mcp` needs `ecorp:read`, `/ops/mcp` needs `ops:read`, and ops data (deployment-wide) is admitted only for `MCP_GATEWAY_OPS_TENANTS` (empty means nobody).
- **Keys** are refreshed on an unknown `kid` at most once per interval and never trusted past the TTL; when they cannot be fetched a token is refused, `McpGatewayCannotCheckTokens` fires and `/readyz` goes 503.
- **Stateless**, so any replica answers any request and scaling is a flag.
- **Not built:** token revocation (a token is good for its hour) and a stale-key grace. **No write tool or sandbox is behind it:** every side effect needs the approval gate in the agent's graph (Principle II), a decision (specs/017 T022), not a gap.

### 84. The front door checks the token once and hands the API a short-lived signed assertion; a path the proxy calls public never reaches the check behind it
Caddy asks `services/authlimit` about every request (`forward_auth`) before the chat API or an MCP route. It verifies the Casdoor token **offline**, charges per-tenant and per-person token buckets, refuses revoked people and returns a **30-second Ed25519-signed identity** (`X-Internal-Identity`), which Caddy copies onto the request after removing `Authorization`. Asymmetric on purpose: a shared HMAC secret would let any consumer mint an identity for any tenant. The API's half is 86.
- **Bug (found by the real-stack smoke):** a path the proxy declared public never reached the check (`/static/a%5c..%5cchat/resume` matched `/static/*` and went through with no token). **Whichever component declares a route public must be the strictest reader of the URL**; only plain URLs are public now.
- **Bug:** a dead replica cost the dial timeout on every request. One failed connect now skips it for 20 s; dial timeout 1 s.
- **MCP routes are limit-only:** the gateway verifies the token itself, so the service verifies only enough to know who is calling and **never refuses a token the gateway accepts**. The 37 shared vectors (`testdata/auth-vectors/`) run in both verifiers.
- **Two failure policies:** a security check fails closed (503), a protection fails open (limiter store down: served, counted, alerted).
- **Not built:** a production identity provider, mutual TLS to the service, a managed key store.

### 85. Revocation is a local copy refreshed on a short interval, so a store outage neither stalls a request nor un-revokes anyone
Access tokens last seven days and are verified offline, so `authlimit revoke` records "tokens issued to this person at or before T are void" (no issue time counts as 0: the safe direction).
- **Each replica copies the list into memory every second; a request is a map lookup.** A Redis call per request would tie the store's health to latency and force one failure policy on two things needing opposite ones (limiter open, revocation closed).
- A replica that cannot refresh keeps enforcing its copy; only past 30 s does the policy apply. It runs before the limiter, so a revoked token cannot drain its tenant's bucket. First refusal 0.3-0.5 s after revoke, every replica within 3.2 s.
- **Not built:** tenant-wide revocation, introspection against the authorization server.

### 86. The API builds its caller from the identity the front door signed and nothing a client sends; keys it cannot confirm are a 503, never a guess
`get_ctx` once read two headers any client can set, so tenant A could send `X-Tenant-Id: B` and be served B's data.
- **Two modes, chosen by name:** `AUTH_MODE=identity` (default) builds the ctx from the verified `tenant` and `sub` alone; `headers` is for the hermetic tests and a local API. It announces itself (startup warning, `agent_api_auth_mode`, alert `ApiHeaderAuthMode`).
- **The check never raises** (`app/platform/internal_identity.py`): a `Refusal` with a closed-list reason, counted. EdDSA from an allow-list; `typ`, `iss`, `aud`, `exp`, `nbf` checked. A refusal never says why.
- **Keys it cannot confirm are a 503 `auth_unavailable`, not a 401** (a 401 sends a logged-in person to sign in during an outage); cached, refetched at most once per 10 s, kept 600 s through a failed refresh, then closed. The API refuses to start in identity mode without `AUTHLIMIT_KEYS_URL`.
- **The web page signs in itself** (`static/auth.js`): authorization code + PKCE, the code removed from the address bar, the token in `sessionStorage`, `resource` on both the authorize request and the exchange (only a real browser run showed Casdoor needs it).
- **Verified three ways:** `make authlimit-smoke ARGS="--upstream api"` (a forged header and a validly signed forged identity change nothing), `make authlimit-web-check` (Chromium), and the page's script against a DOM stub.
- **Not built:** a refresh token in the page, a session cookie, a CSP. `GET /ingest/stream/{job_id}` asks for no caller: any signed-in person who knows a job id can follow its progress.

### 97. A request with no token is charged to the network address Caddy resolved, and a protection that cannot read the address limits everyone, not no one
Sign-in and webhooks carry no token, so the door never saw them, and the app's own per-address limiter, behind a proxy, keyed on the proxy.
- `/v1/limit/signin` and `/v1/limit/webhook` verify nothing and charge one bucket per address (`address_limits` in `services/authlimit/routes.json`). The sign-in host charges only `/api/login*`, not the page and assets.
- **The address is Caddy's and cannot be set by a client:** `header_up X-Client-Ip {client_ip}` SETS it. **An unreadable address is one shared bucket, not no bucket**, so a Caddy that stopped sending it limits people loudly instead of switching the protection off. An IPv6 client is its /64.
- Same script as the token buckets (one copy of the refill arithmetic). A path trick (`/api//login`) can cost a token, never skip the limit.
- **Disclosed:** it bounds the *rate* one address can try, not lockout of a person (Casdoor locks after five wrong passwords); addresses behind one NAT share a bucket; the Telegram bot is one address for all its people, so `signin` (60 burst, 120/min) is its ceiling.

### 98. Production runs the same front door as development, from one routing file; the entries only say how a site is addressed and what sits behind it
Production ran `api` behind a Caddy that did not authenticate. Copying the dev Caddyfile would have left two copies of the part that decides who gets in.
- **One routing file, two thin entries:** `deploy/shared/caddy/gateway.caddyfile` decides every request; `deploy/dev/caddy/Caddyfile` and `deploy/prod/caddy/Caddyfile` say how each site is addressed and certified.
- **Same services and images; places and passwords are required:** `casdoor`, `authlimit` x2, `mcp-gateway` and the opt-in `telegram-bot` are in `deploy/prod/` (`app.yml`, `gateway.yml`); `API_DOMAIN`, `AUTH_DOMAIN`, `MCP_DOMAIN` and the Casdoor passwords have no default, and issuer, audience and keys URL derive from them in one place.
- **Found by running it:** a `${VAR:?}` on the opt-in bot would have broken `pull` and `up` for everyone ([ENGINEERING_PATTERNS.md](ENGINEERING_PATTERNS.md) 83); a bare `tls` is a Caddy parse error; a Caddy waiting on authlimit's health can never finish a first deploy; the deploy workflow curled a port `api` never publishes.
- **Gaps:** the admin UI shares the sign-in host (admin password and the per-address limit only); nothing was run on a real droplet or against Let's Encrypt.
