# Tool patterns: data access, MCP, the sandbox and web search

How tools reach data and the outside world without becoming a way around the approval gate or tenant isolation. The gate itself (every non-`read_only` call is approved) is GRAPH_PATTERNS.md 15. Numbers are shared with the other pattern documents ([index](README.md#pattern-numbers)).

| # | Lesson |
|---|---|
| 21 | Fixed-tool structured data access, plus MCP exposure |
| 28 | Consuming a remote MCP tool catalog |
| 32 | Credential scrubbing on tool output |
| 50 | Web crawling (crawl4ai) and a sandbox (OpenSandbox over MCP) |
| 78 | A published MCP server acts for whoever launched it, and a remote server is untrusted input |
| 87 | A web search is a read that sends text out and spends money, so it waits for a human |

### 21. Fixed-tool structured data access, plus MCP exposure
`query_employees` is a closed, typed Postgres query: `tenant` (from `SecurityCtx`), a `department` enum and `name_contains` are the only variables, every query is parameterized and ANDs `WHERE tenant = %s`. There is no `execute(sql)`: the access boundary lives in reviewable code, not in a string the model writes. The MCP server wraps the same query under a separate trust boundary (78).
- **Use when:** HR lookups, order status, inventory counts, wherever text-to-SQL is tempting and exactly wrong.

### 28. Consuming a remote MCP tool catalog
`load_remote_tools(...)` (`app/adapters/outbound/mcp/client.py`) connects to an MCP server and wraps each tool as a `StructuredTool`.
- **`capability_overrides` is the only source of truth.** MCP annotations are self-reported hints; an unnamed tool defaults to `outward`, so the approval gate applies with no special case.
- **Async only, one shared connection per server and loop** (`connection.py`). What the remote says is input, not fact (78).

### 32. Credential scrubbing on tool output
`app/core/scrubbing.py`, at the one chokepoint every tool result passes: static patterns for credential shapes (API keys, AWS ids, `password=`, `user:password@`, JWTs) plus this deployment's own configured secrets. Tool output specifically, because a raw DB row can carry a credential into `ToolMessage.content` without passing through the model's input. Fails open (logged).

### 50. Web crawling (crawl4ai) and a sandbox (OpenSandbox over MCP)
crawl4ai (`app/adapters/outbound/crawl4ai/web_crawler.py`) renders JS-heavy pages; OpenSandbox, consumed over MCP through 28, runs code in isolation. Both go through the SSRF guard (`app/core/url_safety.py`) and every tool is `outward`.
- **Wrap narrow:** a 3B model given OpenSandbox's ~19 tools hallucinated a `sandbox_id`. `app/domains/shared/sandbox_session.py` does lookup and creation in code and exposes four flat tools; `run_python_in_sandbox` takes the script as a plain argument because the model broke shell quoting.
- **Bug:** two simultaneous tool calls arrived glued into one entry. `parallel_tool_calls=False` was a no-op; the fix is the LiteLLM chunk-index patch (`deploy/dev/litellm-patches/sitecustomize.py`), which is load-bearing.
- **Bug:** a many-to-one rewrite of a Telegram id let two tenants share a sandbox (Principle I). Tenant and conversation id are now separate SHA-256 tags, filtered on both, and `tenant` is a required keyword on every sandbox helper.
- **Not verified against a real service:** that the sandbox honours the metadata filter and its "no network egress" claim (spec 009, A2).

### 78. A published MCP server acts for whoever launched it, and a remote server is untrusted input
One rule runs through both directions (`app/adapters/inbound/mcp/` publishes, `app/adapters/outbound/mcp/` consumes): the other side of the pipe is not where a trust decision gets made.
- **Bug:** the model chose the tenant. `query_employees` took `tenant` and `principal` as arguments, so a page a connected assistant had read could name another tenant. Identity is now bound at process start (`MCP_TENANT`, `MCP_PRINCIPAL`); no tool exposes either; a server started without one exits before serving.
- **A remote catalog is pinned.** A tool description is text the model obeys, which is where tool-poisoning lives. `pinned` maps each expected tool to a hash of its name, description and schema (`tool_fingerprint`): an unlisted tool is dropped, a changed one is dropped and counted (`McpToolCatalogChanged`).
- **What comes back is rendered, scrubbed, then bounded** (`app/core/output_limits.py`). Scrub before cutting, or a key straddling the cut splits into halves no pattern recognises.
- **Never a second send of a call that may have run** (`connection.py`). mcp 2.x cannot tell a call that never left from one the server ran and died on, so the connection pings first; a call in flight is NOT replayed (Principle IV).
- **mcp 2.x because `opensandbox-mcp` 1.1 is.** The SDK's `Client` class is not used: its protocol revision removes `ping`, which the no-replay guard rests on. SDK, bridge, server and sidecar images move together, with digests.
- **Gap:** nothing authenticates whoever starts a published server over stdio; the networked case is ACCESS_PATTERNS.md 82.

### 87. A web search is a read that sends text out and spends money, so it waits for a human; what comes back is data and must not look like a citation
`web_search` (serper.dev: `app/adapters/outbound/web/serper.py`, tool in `app/domains/shared/web_search.py`) is the missing first half of the page-reading tools in support, ops and sales.
- **`outward`, though it changes nothing:** the query is model-written text sent to a third party and every call spends a credit. It is wrapped in `idempotent()`, so a replay returns the stored result.
- **Never numbered `[1] [2]`:** those are the knowledge base's (`graph/citations.py`). A result is a title, an address and a snippet, framed with `frame_untrusted`; a bracketed number in a snippet becomes `(n)`.
- **Only a connection that never opened is retried** (a read timeout may have been billed). A blank query is refused (it was billed). Provider error text never reaches the model.
- **Always declared, even with no key:** the tool list must not change between deploys, or a thread paused on it could not resume. With no key it answers "web search is not set up".
- `agent_web_search_total{outcome}` and `WebSearchProviderRefusing`; `make web-search-check` makes the one real call (one credit).
- **Gaps:** an out-of-credits account and a 429 were never observed; only `organic` results are read; the spend is not metered (no per-tenant quota or result cache); nothing scans the query for customer data beyond the prompt and the human at approval.
