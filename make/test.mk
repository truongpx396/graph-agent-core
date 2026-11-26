# Tests, static checks, load tests and the manual smoke checks. Included by the Makefile.

##@ Tests

test:  ## Hermetic test suite in parallel, then the billing package's tests in a separate process
	pytest -n auto -q
	cd packages/billing && pytest -q

test-billing-package:  ## The billing package's hermetic tests alone
	cd packages/billing && pytest -q

# --dist=loadgroup: test_worker_scaling.py's xdist_group marker is ignored by plain `load`.
test-integration:  ## Real Postgres/Redis/Qdrant/ml-service/crawl4ai via testcontainers (needs Docker, not `make up`)
	pytest -n auto -m integration -q --dist=loadgroup
	cd packages/billing && pytest -m integration -q

test-live: web-build  ## Real Ollama + ml-service + the full app stack via testcontainers, incl. Playwright E2E (needs Docker and Node)
	playwright install --with-deps chromium
	pytest -n auto -m "llm or e2e or crawl" -q

test-sandbox:  ## Real OpenSandbox MCP round trip (needs `make sandbox-up` and opensandbox-mcp); manual, never CI
	pytest -m sandbox -q -s

# Checkout requests and signed webhooks via `stripe listen` / `polar listen`. Needs STRIPE_API_KEY (test mode) and/or POLAR_ACCESS_TOKEN (sandbox) in .env
# plus the provider CLI; skips without them, refuses a live key, creates labelled sandbox objects.
test-provider-sandbox:  ## A payment provider's real SANDBOX (specs/010 T029); manual, never CI
	pytest -c packages/billing/pyproject.toml packages/billing/tests -m provider_sandbox -q -s -p no:xdist

##@ Static checks

lint:  ## ruff over the repo (config in pyproject.toml)
	ruff check .

typecheck:  ## mypy, strict, over app/, scripts/, the billing package and ml-service (config in pyproject.toml)
	mypy

openapi:  ## Rewrite the committed OpenAPI documents from the code (a test fails while they are stale)
	python -m scripts.tools.export_openapi

# Each document sits beside the Dockerfile of the process it describes.
OPENAPI_DOCUMENTS := openapi.json services/ml-service/openapi.json services/authlimit/openapi.json
OPENAPI_BASE      ?= origin/main

# A deliberate break is accepted by one line in .github/openapi-breaking-ignore.txt, the method, the path and the message oasdiff printed
# (`GET /usage api path removed without deprecation`), in the same PR so review sees it. The file holds entries ONLY: oasdiff has no
# comment syntax, so a `#` line is matched like any other (measured on v1.35.0). Delete a line once the change is on main.
# An accepted break still has to raise the MAJOR version: .github/openapi-severity-levels.txt lifts oasdiff's own "a breaking change was
# detected but the major version did not increase" from info to error (also entries only), and the ignore file does not hide it.
openapi-breaking:  ## Fail when a change breaks an OpenAPI client, or does so without a new major version (vs OPENAPI_BASE; needs oasdiff)
	@command -v oasdiff >/dev/null || { echo "oasdiff is not installed: go install github.com/oasdiff/oasdiff@v1.35.0"; exit 2; }
	@rc=0; for doc in $(OPENAPI_DOCUMENTS); do \
		if git cat-file -e "$(OPENAPI_BASE):$$doc" 2>/dev/null; then \
			oasdiff breaking "$(OPENAPI_BASE):$$doc" "$$doc" --fail-on ERR --err-ignore .github/openapi-breaking-ignore.txt --severity-levels .github/openapi-severity-levels.txt || rc=1; \
		else echo "$$doc: not in $(OPENAPI_BASE), nothing to compare"; fi; \
	done; exit $$rc

openapi-version:  ## Fail when a contract change did not raise an OpenAPI document's info.version (vs OPENAPI_BASE; needs oasdiff)
	python -m scripts.tools.openapi_version --base $(OPENAPI_BASE)

##@ Load tests

# Safe to re-run; the background process logs to var/fake-llm.log.
loadtest-up:  ## (Re)start the fake LLM in the background and point the containerized api and workers at it; undo: loadtest-down
	pkill -f 'loadtest.fake_llm_server' 2>/dev/null || true
	for i in $$(seq 1 20); do \
		pgrep -f 'loadtest.fake_llm_server' >/dev/null 2>&1 || break; \
		sleep 0.5; \
	done
	mkdir -p var
	nohup $(MAKE) fake-llm > var/fake-llm.log 2>&1 &
	for i in $$(seq 1 20); do \
		curl -sf http://localhost:9009/health >/dev/null 2>&1 && break; \
		sleep 0.5; \
	done
	$(MAKE) loadtest-app-up

loadtest-down:  ## Stop the fake LLM and point the api and workers back at real litellm (`make up-app`)
	pkill -f 'loadtest.fake_llm_server' 2>/dev/null || true
	$(MAKE) up-app

loadtest-app-up:  ## Point the running api and workers at the fake LLM through deploy/dev/loadtest.yml (called by loadtest-up)
	$(COMPOSE) -f deploy/dev/loadtest.yml up -d \
		api agent-worker ingest-worker

# Measures agent_worker's own concurrency, not Ollama's.
loadtest-queued:  ## Interactive Locust UI against the queued chat path; needs `make loadtest-up`
	locust -f loadtest/locustfile_queued.py --host http://localhost:8000

loadtest-queued-headless:  ## Headless 20-user, 2-minute run of the same scenario; CSV and HTML report in loadtest/results-queued/
	mkdir -p loadtest/results-queued
	locust -f loadtest/locustfile_queued.py --host http://localhost:8000 \
		--headless --users 20 --spawn-rate 5 --run-time 2m \
		--csv loadtest/results-queued/loadtest --html loadtest/results-queued/report.html

##@ Smoke checks (manual)

# Needs Docker only. Re-run after bumping the image (deploy/README.md).
gateway-check:  ## Run the PINNED LiteLLM image on the dev config and a stub backend: aliases priced, spend = tokens x price, nothing identifying leaks
	python -m scripts.checks.gateway_check

# Prints the outcome and the first result, never the key. Exit 0 ok, 1 failed, 2 no key (GRAPH_PATTERNS.md 87).
web-search-check:  ## Does SERPER_API_KEY work? One real search; spends ONE serper.dev credit
	python -m scripts.checks.web_search_check
