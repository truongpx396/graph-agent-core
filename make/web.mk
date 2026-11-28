# The web client in web/ (React, TypeScript, Vite). Its targets, and what depends on them (`test-live`), are the only ones that need
# Node (see web/.nvmrc). `make serve` shows the page only after `make web-build`; in containers the page is the `web` service's own image (web/Dockerfile).

##@ Web client

web-install:  ## Install the web client's dependencies from its lock file
	cd web && npm ci

web-dev:  ## Dev server with hot reload on :5173, proxying to the API (WEB_API_TARGET, default http://localhost:8000; AUTH_MODE=headers)
	cd web && npm run dev

web-build:  ## Production build into web/dist, which `make serve` serves at / (installs the dependencies first if there are none)
	cd web && { [ -d node_modules ] || npm ci; } && npm run build

web-api:  ## Regenerate the web client's API types from openapi.json (CI fails while they are stale)
	cd web && npm run gen:api

web-check:  ## Everything CI's `web` job runs: generated API types are fresh, lint, type check, tests, build
	cd web && npm run check

web-image-check:  ## Build the web client's image and check what it serves, under the hardening compose applies (needs Docker)
	docker build --target runtime -t graph-agent-core-web:ci web
	python3 -m scripts.checks.web_image graph-agent-core-web:ci
