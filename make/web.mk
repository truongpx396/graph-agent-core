# The web client in web/ (React, TypeScript, Vite). The only targets that need Node (see web/.nvmrc); nothing else here does.

##@ Web client

web-install:  ## Install the web client's dependencies from its lock file
	cd web && npm ci

web-dev:  ## Dev server with hot reload on :5173, proxying to the API (WEB_API_TARGET, default http://localhost:8000; AUTH_MODE=headers)
	cd web && npm run dev

web-build:  ## Production build into web/dist
	cd web && npm run build

web-api:  ## Regenerate the web client's API types from openapi.json (CI fails while they are stale)
	cd web && npm run gen:api

web-check:  ## Everything CI's `web` job runs: lint, types, tests, build
	cd web && npm run check
