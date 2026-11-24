# The dev stack: start, stop, reset, and the observability stack with its collectors. Included by the Makefile.

##@ Stack: start and stop

up:  ## Start the infra: Postgres, Redis, Qdrant, LiteLLM, MinIO, Langfuse, the ML service (needs a native Ollama on the host)
	$(COMPOSE) up -d

up-app:  ## Start the infra plus the containerized API and workers (profile `app`)
	$(COMPOSE) --profile app up -d --build

# One compose call over the app and gateway profiles (not sandbox or quality), in identity mode like authlimit-up-app; observability and its collectors follow.
# Web UI: https://api.localhost:8443/
up-all: mcp-gateway-secrets authlimit-keys-ensure  ## The whole stack in containers behind the front door, plus observability (not the sandbox or SonarQube)
	@test -n "$(COMPOSE_OVERLAYS)" || curl -sf -o /dev/null http://localhost:11434/api/version || echo "warning: no Ollama on :11434 (start it with: ollama serve)"
	AUTH_MODE=identity AUTHLIMIT_UPSTREAM=api:8000 $(GATEWAY_COMPOSE) --profile app up -d --build
	$(MAKE) obs-up
	@echo "up. Web UI https://api.localhost:8443/ · Grafana http://localhost:3300 · Langfuse http://localhost:3000"
	@echo "first time: make mcp-gateway-bootstrap, then make mcp-gateway-ca and trust the CA in your browser"

sandbox-up: sandbox-build  ## Start the OpenSandbox server (profile `sandbox`; mounts the Docker socket; needs OPENSANDBOX_API_KEY)
	$(COMPOSE) --profile sandbox up -d --build

# deploy/images/sandbox.Dockerfile: python + numpy + pandas, non-root, no network.
sandbox-build:  ## Build the sandbox base image SANDBOX_IMAGE names (python, numpy, pandas); rerun after editing its Dockerfile
	docker build -t graph-agent-core-sandbox:latest -f deploy/images/sandbox.Dockerfile .

logs:  ## Tail logs from all services
	$(COMPOSE) logs -f

down:  ## Stop all services (keep volumes), the collectors included
	$(COMPOSE) down --remove-orphans

# `down` skips containers started under a profile; `--profile '*'` reaches them all.
# `--profile '*'` on the observability project too: SonarQube is its `quality` profile, which `obs-down` leaves alone on purpose.
down-all: mcp-gateway-secrets  ## Stop every container of the dev stack, any profile, plus observability and collectors; keeps volumes
	$(COMPOSE_OBS) --profile '*' down
	$(MAKE) collectors-down
	$(GATEWAY_COMPOSE) --profile '*' down

clean:  ## Stop services and delete volumes (models, vectors, traces)
	$(COMPOSE) down -v --remove-orphans

##@ Reset: delete data

clear-cache:  ## Flush the semantic cache (Redis) only — leaves the agent-worker queue and other volumes intact
	$(COMPOSE) exec redis sh -c "redis-cli --scan --pattern 'cache:*' | xargs -r redis-cli del"

# Streams: agent:requests:*, agent:results:*, ingest:requests, ingest:results:*.
clear-streams:  ## Delete every Redis Stream: drops queued and in-flight turns and ingest jobs, keeps the cache and other keys
	$(COMPOSE) exec redis sh -c "redis-cli --scan --pattern '*' | while read -r k; do [ \"\$$(redis-cli type \"\$$k\")\" = stream ] && redis-cli del \"\$$k\"; done"

clear-checkpoints:  ## Truncate the checkpointer's tables: drops all saved conversation state, nothing else
	$(COMPOSE) exec postgres psql -U langfuse -d checkpointer -c "TRUNCATE checkpoints, checkpoint_blobs, checkpoint_writes;"

clear-langfuse:  ## Truncate Langfuse's trace tables; projects and API keys stay, so LANGFUSE_*_KEY still works
	$(COMPOSE) exec postgres psql -U langfuse -d langfuse -c "TRUNCATE traces, observations, scores, trace_sessions, events, comments, media, trace_media, observation_media CASCADE;"

clear-litellm:  ## Truncate LiteLLM's spend, error and audit logs; users, teams, keys and models stay
	$(COMPOSE) exec postgres psql -U langfuse -d litellm -c "TRUNCATE \"LiteLLM_SpendLogs\", \"LiteLLM_SpendLogToolIndex\", \"LiteLLM_SpendLogGuardrailIndex\", \"LiteLLM_ErrorLogs\", \"LiteLLM_AuditLog\", \"LiteLLM_DailyUserSpend\", \"LiteLLM_DailyTeamSpend\", \"LiteLLM_DailyTagSpend\", \"LiteLLM_DailyEndUserSpend\", \"LiteLLM_DailyOrganizationSpend\", \"LiteLLM_DailyAgentSpend\", \"LiteLLM_DailyToolSpend\", \"LiteLLM_DailyGuardrailMetrics\", \"LiteLLM_DailyPolicyMetrics\" CASCADE;"

clear-all: clear-cache clear-streams clear-checkpoints clear-langfuse clear-litellm  ## Run every clear-* target; leaves appdata and volumes (`make clean` deletes volumes)

##@ Observability and collectors

obs-up:  ## Start observability (Grafana :3300, Prometheus :9090, Loki, Alertmanager, Pyroscope) and its collectors
	$(COMPOSE_OBS) up -d
	$(MAKE) collectors-up

obs-down:  ## Stop observability and the collectors (keep volumes)
	$(COMPOSE_OBS) down
	$(MAKE) collectors-down

obs-logs:  ## Tail logs from the observability stack
	$(COMPOSE_OBS) logs -f

obs-clean:  ## Stop observability and the collectors and delete the observability volumes
	$(COMPOSE_OBS) down -v
	$(MAKE) collectors-down

collectors-up:  ## Start the telemetry collectors (OTel collector, exporters, cAdvisor, Promtail) beside `make up`
	$(COMPOSE_COLLECTORS) up -d --no-deps $(COLLECTORS)

collectors-down:  ## Stop and remove the telemetry collectors
	$(COMPOSE_COLLECTORS) rm -sf $(COLLECTORS)

collectors-logs:  ## Tail logs from the telemetry collectors
	$(COMPOSE_COLLECTORS) logs -f $(COLLECTORS)
