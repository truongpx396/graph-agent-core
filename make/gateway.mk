# The front door: Casdoor, authlimit and Caddy, and the MCP gateway behind it (specs/017, 018). The variables are in the Makefile, because the admin and test
# targets use them too. Included by the Makefile.

##@ Front door: start and stop

front-door-secrets:  # internal: create the generated passwords once (never overwritten), after moving the folder an older checkout made
	@if [ -d $(FRONT_DOOR_DIR_OLD) ] && [ ! -e $(FRONT_DOOR_DIR) ]; then mv $(FRONT_DOOR_DIR_OLD) $(FRONT_DOOR_DIR) && echo "moved $(FRONT_DOOR_DIR_OLD) to $(FRONT_DOOR_DIR) (the Casdoor passwords an existing stack needs are in it)"; fi
	@mkdir -p $(FRONT_DOOR_DIR) && chmod 700 $(FRONT_DOOR_DIR)
	@test -f $(FRONT_DOOR_SECRETS) || { ( umask 077; printf 'CASDOOR_DB_PASSWORD=%s\nCASDOOR_ADMIN_PASSWORD=%s\n' "$$(openssl rand -hex 16)" "$$(openssl rand -hex 16)" > $(FRONT_DOOR_SECRETS) ) && echo "created $(FRONT_DOOR_SECRETS) (the Casdoor admin password is in it)"; }

authlimit-keys-ensure:  # internal: create the first signing key if there is none (never replaces one)
	@test -f $(AUTHLIMIT_KEYS_DIR)/keyset.json || $(MAKE) --no-print-directory authlimit-keys

# specs/017, 018: Casdoor and its Postgres, authlimit, Caddy with local TLS and the OAuth-protected MCP gateway behind them. Then `make front-door-bootstrap`.
front-door-up: front-door-secrets authlimit-keys-ensure  ## Start the front door (Casdoor, authlimit, Caddy on https://*.localhost:8443) and the MCP gateway behind it; needs `make up`
	$(GATEWAY_COMPOSE) up -d --build --no-deps casdoor-db casdoor mcp-gateway authlimit authlimit-echo caddy-gateway

# Idempotent; ARGS="--tenant acme --tenant other-co" adds tenants (default ecorp).
front-door-bootstrap: front-door-secrets  ## Create the Casdoor organizations, the chat API's and MCP's applications and the demo users; prints the `claude mcp add` commands: ARGS="--tenant acme"
	@. $(FRONT_DOOR_SECRETS) && export CASDOOR_ADMIN_PASSWORD && $(GATEWAY_COMPOSE) exec -T -e CASDOOR_ADMIN_PASSWORD mcp-gateway python -m scripts.tools.front_door_bootstrap $(ARGS)

front-door-ca: front-door-secrets  ## Export Caddy's local root certificate so a client can trust https://*.localhost:8443
	$(GATEWAY_COMPOSE) cp caddy-gateway:/data/caddy/pki/authorities/local/root.crt $(FRONT_DOOR_DIR)/caddy-root.crt
	@echo "export NODE_EXTRA_CA_CERTS=$(FRONT_DOOR_DIR)/caddy-root.crt   # before starting Claude Code"

front-door-down: front-door-secrets  ## Stop and remove the front door's containers, the MCP gateway's included (its volumes stay)
	$(GATEWAY_COMPOSE) rm -sf caddy-gateway authlimit authlimit-echo mcp-gateway casdoor casdoor-db

# These were `mcp-gateway-*` until the front door was more than the MCP gateway's (specs 017, 018). The old names still work, so a spec's quickstart or a habit does not break;
# no `##`, so `make help` lists the new names once. `mcp-gateway-smoke` keeps its name: it is the one check that is about MCP.
mcp-gateway-up: front-door-up
mcp-gateway-down: front-door-down
mcp-gateway-ca: front-door-ca
mcp-gateway-bootstrap: front-door-bootstrap

authlimit-build:  ## Build the authlimit service image (distroless, non-root, no shell)
	docker build -t authlimit:dev services/authlimit

# Runs as you, so the key files are yours (mode 600).
authlimit-keys: authlimit-build  ## Manage the signing keys outside the repo: ARGS=init (default) | "rotate --overlap 10m" | list
	@mkdir -p $(AUTHLIMIT_DIR) && chmod 700 $(AUTHLIMIT_DIR)
	@docker run --rm --user "$$(id -u):$$(id -g)" -v $(AUTHLIMIT_DIR):/work authlimit:dev keys $(or $(ARGS),init) --dir /work/keys

authlimit-up: front-door-up  ## Alias of front-door-up (Casdoor, authlimit x2, the MCP gateway, an echo stub, Caddy)

# specs/018. Needs `make up`, `make front-door-bootstrap` and `make front-door-ca`; web UI https://api.localhost:8443/.
authlimit-up-app: front-door-secrets authlimit-keys-ensure  ## Like authlimit-up, with the real API (identity mode) and a worker behind the door instead of the echo stub
	AUTH_MODE=identity AUTHLIMIT_UPSTREAM=api:8000 $(GATEWAY_COMPOSE) --profile app up -d --build --no-deps casdoor-db casdoor authlimit authlimit-echo mcp-gateway api agent-worker caddy-gateway

# The next `--profile app up` brings the API back in header mode.
authlimit-down-app: front-door-secrets  ## Stop what authlimit-up-app started, the API and its worker included
	$(GATEWAY_COMPOSE) --profile app rm -sf caddy-gateway authlimit authlimit-echo mcp-gateway casdoor casdoor-db api agent-worker

authlimit-down: front-door-down  ## Stop the authlimit stack's containers (same as front-door-down)

# The bot as a container (profile `telegram`), the shape production runs it in. It reaches the front door by its public names, which Caddy's aliases resolve to Caddy itself, and trusts
# Caddy's local CA from the certificate `front-door-ca` exports. Caddy is recreated so /telegram/* goes to the container; `make front-door-up` later sends it back to a host bot.
telegram-up: front-door-ca  ## Run the Telegram bot as a container behind the front door (needs `make front-door-up` and TELEGRAM_BOT_TOKEN)
	TELEGRAM_UPSTREAM=telegram-bot:8081 TELEGRAM_CA_FILE=$(FRONT_DOOR_DIR)/caddy-root.crt $(GATEWAY_COMPOSE) --profile telegram up -d --build --no-deps telegram-bot caddy-gateway

telegram-down: front-door-secrets  ## Stop and remove the Telegram bot container
	$(GATEWAY_COMPOSE) --profile telegram rm -sf telegram-bot

##@ Front door: checks (manual)

authlimit-test:  ## go vet and the authlimit service's tests with the race detector (specs/018; needs Go, no Docker)
	cd services/authlimit && go vet ./... && go test -race ./...

# CI fails when the committed file differs.
authlimit-generate:  ## Regenerate services/authlimit/internal/api from its openapi.json with oapi-codegen (needs Go and the network)
	cd services/authlimit && go generate ./...

# Run after `make front-door-ca` and `make front-door-bootstrap`, and after any change to the Casdoor digest.
mcp-gateway-smoke: front-door-secrets  ## End-to-end OAuth + MCP check against the running gateway stack (throwaway users); manual, never CI
	@. $(FRONT_DOOR_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.checks.mcp_gateway_smoke --ca $(FRONT_DOOR_DIR)/caddy-root.crt

# Throwaway users. Run after `make authlimit-up`, `make front-door-ca` and `make front-door-bootstrap`.
authlimit-smoke: front-door-secrets  ## End-to-end check of the front door: sign-in, refusal, rate limit, revocation, replica kill, Casdoor outage, key rotation; manual
	@. $(FRONT_DOOR_SECRETS) && export CASDOOR_ADMIN_PASSWORD && $(GATEWAY_ENV) python -m scripts.checks.authlimit_smoke --ca $(FRONT_DOOR_DIR)/caddy-root.crt --keys-dir $(AUTHLIMIT_KEYS_DIR) --compose '$(GATEWAY_COMPOSE_CMD)' $(ARGS)

# specs/018. Run after `make authlimit-up-app`, `make front-door-bootstrap ARGS="--tenant ecorp --tenant other-co"` and `make front-door-ca`,
# with the worker on loadtest/fake_llm_server.py.
authlimit-web-check:  ## The web page in a real browser (Playwright): sign in through Casdoor, chat, tenant isolation, sign out; manual
	@. $(FRONT_DOOR_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.checks.authlimit_web_check --ca $(FRONT_DOOR_DIR)/caddy-root.crt $(ARGS)

# A checked route against a public one, alternating. Run after `make authlimit-up` and `make front-door-bootstrap`.
authlimit-latency:  ## Time what the front door's check adds to a request (specs/018 SC-008): ARGS="-n 1000 --path /chat/sessions"
	@. $(FRONT_DOOR_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.checks.authlimit_latency --ca $(FRONT_DOOR_DIR)/caddy-root.crt $(ARGS)

# Needs Go.
authlimit-mutants:  ## Apply each deliberate authlimit defect and require a test to fail (specs/018 SC-010); edits source in place, run on a clean tree
	python scripts/checks/authlimit_mutants.py
