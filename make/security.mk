# Security scanners, SonarQube (local only, not in CI) and DefectDojo. Included by the Makefile. CI's security.yml and zap.yml run these same targets.

##@ Scanners

SEMGREP_FLAGS ?= --error
semgrep:  ## SAST over the Python, Go and Dockerfile source (CI's gate; SEMGREP_FLAGS="--sarif --output x.sarif" reports instead)
	docker run --rm -v $(CURDIR):/src -w /src semgrep/semgrep:1.177.0 semgrep scan --metrics=off \
		--config p/security-audit --config p/secrets --config p/python --config p/golang --config p/dockerfile \
		$(SEMGREP_FLAGS) app/ scripts/ packages/ services/ deploy/images/ Dockerfile web/Dockerfile

# The gate: fixable CRITICAL vulnerabilities, secrets and misconfigurations. Scans requirements-lock.txt, not requirements.txt: version ranges
# resolve to no installed version, so that finds nothing. deploy/terraform is Checkov's: Trivy flags the necessary public 80/443 ingress there
# as CRITICAL. .trivyignore.yaml holds dated, reasoned exceptions only. The report does not read it, so an exception stays visible.
TRIVY_FLAGS ?= --severity CRITICAL --ignore-unfixed --exit-code 1 --ignorefile .trivyignore.yaml
trivy:  ## Dependencies, secrets and Dockerfile/compose misconfiguration (CI's gate; TRIVY_FLAGS replaces the policy)
	docker run --rm -v $(CURDIR):/repo -w /repo aquasec/trivy:0.74.0 fs \
		--scanners vuln,secret,misconfig --file-patterns 'pip:requirements-lock\.txt$$' \
		--skip-dirs .venv,**/node_modules,.git,.mypy_cache,.ruff_cache,.pytest_cache,deploy/terraform $(TRIVY_FLAGS) .

trivy-image:  ## Build the app image and scan it for OS/library vulnerabilities
	docker build -t graph-agent-core:trivy .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.74.0 image \
		--severity HIGH,CRITICAL --ignore-unfixed graph-agent-core:trivy

checkov:  ## IaC scan of deploy/terraform, same config and gate as CI (pip, not Docker)
	pip install -q checkov==3.3.17
	checkov --config-file .checkov.yaml

##@ SonarQube

# Its volumes are the observability project's, so a SonarQube from before deploy/dev/ starts empty. Change the admin password before a real scan.
sonar-up:  ## Start a persistent SonarQube at http://localhost:9002 (`quality` profile of the observability project; first login admin/admin)
	$(COMPOSE_OBS) --profile quality up -d sonarqube-db sonarqube

sonar-down:  ## Stop the persistent SonarQube (volumes and history kept)
	$(COMPOSE_OBS) --profile quality stop sonarqube sonarqube-db

# My Account -> Security makes the token. The baseline scan found ~90 older issues (2 BLOCKER, 10 CRITICAL) to triage.
# Joins the observability project's network and uses the in-network sonarqube:9000, not --network host: host networking is unreliable on macOS.
sonar-scan:  ## Scan against the `make sonar-up` server (needs SONAR_TOKEN); the gate blocks only NEW issues
	@test -n "$$SONAR_TOKEN" || (echo "SONAR_TOKEN is required — generate one in the SonarQube UI (My Account -> Security)" && exit 1)
	docker run --rm --network $(OBS_NETWORK) \
		-e SONAR_HOST_URL=http://sonarqube:9000 -e SONAR_TOKEN=$$SONAR_TOKEN \
		-v $(PWD):/usr/src -w /usr/src \
		sonarsource/sonar-scanner-cli@sha256:23ca0f137965d9dff2198074043fd48d386280bc5d0ccac8c8349cea4cf096a9

##@ Dynamic and AI pentest (only systems you own or may test)

# Reports in zap_reports/ (HTML, and the XML `make defectdojo-import` and zap.yml's import read). The same image everywhere, bumped by hand.
# The image runs as its own `zap` user, so the report directory must be writable by others, or on Linux no report is written.
# --add-host: a bare `docker run` is not on the compose network, and native Linux Docker does not map
# host.docker.internal (Docker Desktop does), which the `api` service gets through extra_hosts.
ZAP_IMAGE := zaproxy/zap-stable:2.17.0@sha256:9624669db72f54d86c4b46a7064518bd9bf79d8c61a4c31befa9923489f328af
zap_run    = mkdir -p zap_reports && chmod a+w zap_reports && docker run --rm --add-host=host.docker.internal:host-gateway \
	-v $(CURDIR)/zap_reports:/zap/wrk:rw -t $(ZAP_IMAGE)

zap-baseline:  ## OWASP ZAP baseline scan (spider + passive rules) of a running target: ZAP_TARGET=<url> (default the local API)
	$(zap_run) zap-baseline.py -t "$${ZAP_TARGET:-http://host.docker.internal:8000}" -x baseline-report.xml -r baseline-report.html -I

zap-api-scan:  ## OWASP ZAP ACTIVE scan driven by the OpenAPI spec (needs the API up); only against a target you own or may test
	$(zap_run) zap-api-scan.py -t "$${ZAP_TARGET:-http://host.docker.internal:8000}/openapi.json" -f openapi -x api-scan.xml -r api-scan.html -I

zap-view:  ## Open the newest ZAP HTML report (from `make zap-baseline` / `make zap-api-scan`)
	open "$$(ls -t zap_reports/*.html | head -1)" 2>/dev/null || xdg-open "$$(ls -t zap_reports/*.html | head -1)"

# Strix reads neither `.env` nor anything `make` can hand it, only the shell's exports. Load just its five documented names from `.env`,
# not the whole file (it also holds OPENAI_API_BASE, which Strix would take as LLM_API_BASE, and secrets it has no use for; those aliases
# are unset too). A non-empty `.env` value beats an exported one; values are read literally (one pair of quotes stripped), never evaluated.
STRIX_ENV = for n in STRIX_LLM LLM_API_KEY LLM_API_BASE EXA_API_KEY PERPLEXITY_API_KEY; do v=$$(sed -n -E "s/^$$n=(.+)$$/\1/p" .env 2>/dev/null | tail -n 1); case "$$v" in \"*\") v=$${v\#\"}; v=$${v%\"};; \'*\') v=$${v\#\'}; v=$${v%\'};; esac; [ -n "$$v" ] && export "$$n=$$v"; done; env -u OPENAI_API_BASE -u LITELLM_BASE_URL -u OLLAMA_API_BASE

# STRIX_LLM / LLM_API_KEY in .env or the shell; extra flags via ARGS="--instruction '...'".
strix:  ## Autonomous AI pentest of this repo's SOURCE (needs Docker, strix-agent and a CLOUD LLM key); only systems you own
	$(STRIX_ENV) strix --target . --scan-mode quick $(ARGS)

# Needs `make up`, `make serve` or `make up-app`, and `make ingest` for data to probe.
strix-app:  ## The same tool, black-box against the RUNNING app and its OpenAPI spec
	$(STRIX_ENV) strix --target http://localhost:8000/openapi.json --target http://localhost:8000 --scan-mode quick $(ARGS)

strix-view:  ## Open the local dashboard (findings, repro steps) for the last `make strix` / `make strix-app` run
	strix view

##@ DefectDojo

# Pinned DefectDojo release, cloned into ~/.cache rather than run from docker-compose.yml: a 6-container app with its own release
# cadence. DJANGO_VERSION/NGINX_VERSION are set to match; both 3.3.100 images exist on Docker Hub (the compose file defaults to `:latest`).
DEFECTDOJO_VERSION := 3.3.100
DEFECTDOJO_DIR := $(HOME)/.cache/graph-agent-core-defectdojo

# The first run clones the pinned release into ~/.cache/graph-agent-core-defectdojo.
defectdojo-up:  ## Start a persistent DefectDojo (http://localhost:8080); the first run prints the one-time admin password: save it
	@mkdir -p $(HOME)/.cache
	@test -d "$(DEFECTDOJO_DIR)" || git clone --branch $(DEFECTDOJO_VERSION) --depth 1 https://github.com/DefectDojo/django-DefectDojo.git "$(DEFECTDOJO_DIR)"
	cd "$(DEFECTDOJO_DIR)" && DJANGO_VERSION=$(DEFECTDOJO_VERSION) NGINX_VERSION=$(DEFECTDOJO_VERSION) docker compose up -d
	@echo "Waiting for the initializer (first run: DB migration + seed data, up to ~3 min)..."
	@for i in $$(seq 1 60); do \
		cd "$(DEFECTDOJO_DIR)" && docker compose logs initializer 2>/dev/null | grep -q "Admin password:" && break; \
		sleep 5; \
	done
	@cd "$(DEFECTDOJO_DIR)" && docker compose logs initializer 2>/dev/null | grep "Admin password:" || echo "(already initialized on a previous run — admin password was only ever printed once; see DefectDojo's own docs to reset it if lost)"
	@echo "DefectDojo: http://localhost:8080 (user: admin) — generate an API v2 Key under My Account for \`make defectdojo-import\`"

defectdojo-down:  ## Stop the persistent DefectDojo (volumes and findings kept)
	cd "$(DEFECTDOJO_DIR)" && docker compose stop

defectdojo-import:  ## Import a scan report: DEFECTDOJO_API_KEY=... REPORT=<path> SCAN_TYPE="<scan_type>" [ENGAGEMENT=<name>]
	@test -n "$$DEFECTDOJO_API_KEY" || (echo "DEFECTDOJO_API_KEY is required — generate one in the DefectDojo UI (My Account -> API v2 Key)" && exit 1)
	@test -n "$$REPORT" || (echo "REPORT=<path to scan report file> is required" && exit 1)
	@test -n "$$SCAN_TYPE" || (echo 'SCAN_TYPE="<DefectDojo scan_type string>" is required, e.g. SCAN_TYPE="ZAP Scan"' && exit 1)
	python -m scripts.tools.defectdojo_import --file "$$REPORT" --scan-type "$$SCAN_TYPE" $${ENGAGEMENT:+--engagement-name "$$ENGAGEMENT"}
