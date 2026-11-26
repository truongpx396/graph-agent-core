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
	$(MAKE) test-live-models
	$(MAKE) test-live-e2e
	-$(MAKE) test-live-advisory

# Three separate pytest processes, never one `-m "llm or e2e or crawl"` run: the `llm`/`crawl` tests are async (pytest-asyncio) and the `e2e`
# tests drive Playwright's sync API, which runs its own loop. One xdist worker given both is left with a broken asyncio state and every later
# async test on it fails ("Runner.run() cannot be called from a running event loop"). CI's `test-live` job runs these three.
test-live-models:  ## The `llm` and `crawl` tests, in parallel
	pytest -n auto -m "llm or crawl" -q

# No `-n`: the real-stack fixtures are per xdist worker, so several workers would share one cached Redis and contend for its Streams.
test-live-e2e:  ## The gating browser tests, serially (every one has passed unless something real broke)
	pytest -m "e2e and not advisory" -q

# The two multi-step tests need a 3B model to chain 3+ calls (GRAPH_PATTERNS.md pattern 48): they report, they do not gate.
test-live-advisory:  ## The advisory browser tests
	pytest -m "e2e and advisory" -q

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

# zizmor reads .github (workflows, actions, dependabot); --offline so the result depends on the commit alone.
lint-workflows:  ## actionlint, zizmor and shellcheck over the workflows and shell scripts (pip install -r requirements-lint.txt)
	actionlint
	zizmor --offline .github
	shellcheck $$(git ls-files '*.sh' ':!.specify/')

compose-check:  ## `docker compose config` on every dev and production project and overlay (starts nothing)
	python3 -m scripts.tools.compose_check

# The images production runs, read from its compose file, so a check never validates against a different version than the one deployed.
PROM_IMAGE = $(shell sed -nE 's|^ *image: (prom/prometheus:[^ ]+).*|\1|p' deploy/prod/observability.yml)
AM_IMAGE   = $(shell sed -nE 's|^ *image: (prom/alertmanager:[^ ]+).*|\1|p' deploy/prod/observability.yml)

# Only amtool knows whether a config loads, and a config that does not is an Alertmanager that never starts (spec 008 A7).
monitoring-check:  ## promtool on the Prometheus configs and alert rules, amtool on both Alertmanager configs (needs Docker)
	for config in prometheus.yml prometheus.prod.yml; do \
		docker run --rm --entrypoint promtool \
			-v $(CURDIR)/observability/prometheus/$$config:/etc/prometheus/prometheus.yml:ro \
			-v $(CURDIR)/observability/prometheus/alerts.yml:/etc/prometheus/alerts.yml:ro \
			$(PROM_IMAGE) check config /etc/prometheus/prometheus.yml || exit 1; \
	done
	mkdir -p var && printf '%s' 'https://hooks.slack.com/services/T000/B000/XXXX' > var/amtool_slack_url
	for config in alertmanager.yml alertmanager.prod.yml; do \
		docker run --rm --entrypoint amtool \
			-v $(CURDIR)/observability/alertmanager/$$config:/etc/alertmanager/alertmanager.yml:ro \
			-v $(CURDIR)/var/amtool_slack_url:/tmp/slack_webhook_url:ro \
			$(AM_IMAGE) check-config /etc/alertmanager/alertmanager.yml || exit 1; \
	done

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
