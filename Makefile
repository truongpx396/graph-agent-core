# Shared settings, `make help` and the includes. The targets are in make/*.mk, one file per topic:
#   stack.mk     start, stop and reset the dev stack; observability and collectors
#   data.mk      models and the vector store
#   host.mk      the app as host processes: API, workers, channels
#   gateway.mk   the front door (Casdoor, authlimit, Caddy) and its checks
#   droplet.mk   the dev stack on one DigitalOcean droplet
#   test.mk      tests, static checks, load tests, smoke checks
#   evals.mk     model and prompt evaluation, red team
#   security.mk  scanners, SonarQube, DefectDojo
#   admin.mk     sweeps, billing, budgets, people and tokens

# Python finds the billing package and the ML service's `ml_service` by path, not by install: a path dependency does not belong in the lock file (spec 011 D20).
# Absolute, so a target that `cd`s into packages/billing still finds them; pytest and mypy do the same.
export PYTHONPATH := $(CURDIR)/packages/billing/src:$(CURDIR)/services/ml-service$(if $(PYTHONPATH),:$(PYTHONPATH))

# --- Compose ---
# The compose files are in deploy/dev/ (docker-compose.yml `include`s the parts), but every path in them, the `include`s too, is relative to the repo root, and `.env` is read
# from there. `--project-directory .` pins that: without it the first `include` fails ("no such file"), where the old single file took the project name `compose` and
# orphaned the volumes. It also lets deploy.yml mirror this layout. Consequence: a bare `docker compose up` finds no file.
# COMPOSE_OVERLAYS adds -f files to the dev project: `make droplet-up` passes deploy/dev/hosted-llm.yml, the hosted LLM a droplet needs.
COMPOSE_OVERLAYS ?=
COMPOSE     := docker compose --project-directory . -f deploy/dev/docker-compose.yml $(COMPOSE_OVERLAYS)
COMPOSE_OBS := docker compose --project-directory . -f deploy/dev/observability.yml
# collectors.yml is an overlay on the app's project (these exporters dial postgres, redis and pgbouncer by name, so they need its network), not a project of its own.
# COLLECTORS names its services, so `up --no-deps` and the remove command touch only them. The other telemetry agents are in observability.yml, started by obs-up.
COLLECTORS         := postgres-exporter redis-exporter pgbouncer-exporter
COMPOSE_COLLECTORS := $(COMPOSE) -f deploy/dev/collectors.yml
# Their containers belong to the project but not to its docker-compose.yml: without this every `make up` warns about them as orphans.
export COMPOSE_IGNORE_ORPHANS := true
# SonarQube is in the observability project (profile `quality`), whose name its file fixes, so its network is fixed too: `sonar-scan` joins it.
OBS_NETWORK := agent-core-observability_default

# --- The front door (gateway.mk; here because the admin and test targets use them too) ---
# Casdoor's generated passwords live outside the repo and go to compose as a second --env-file (`.env` first, so every other variable still comes from it).
FRONT_DOOR_DIR     := $(HOME)/.cache/graph-agent-core-front-door
FRONT_DOOR_SECRETS := $(FRONT_DOOR_DIR)/secrets.env
# Where an older checkout kept them, while the front door was only the MCP gateway's (spec 017). `front-door-secrets` moves it: a fresh folder would mint new Casdoor
# database passwords that do not match an existing volume, and Casdoor would not start.
FRONT_DOOR_DIR_OLD := $(HOME)/.cache/graph-agent-core-mcp-gateway
# authlimit's signing keys also live outside the repo, in a directory owned by the host user (the container runs as that user). Never committed, never in an image.
AUTHLIMIT_DIR      := $(HOME)/.cache/graph-agent-core-authlimit
AUTHLIMIT_KEYS_DIR := $(AUTHLIMIT_DIR)/keys
# Recursive (`=`), so they see a COMPOSE the command line overrides (the tests of this stack do). AUTHLIMIT_UID/GID run authlimit as the host user.
GATEWAY_ENV = AUTHLIMIT_UID=$$(id -u) AUTHLIMIT_GID=$$(id -g) AUTHLIMIT_KEYS_DIR=$(AUTHLIMIT_KEYS_DIR)
GATEWAY_COMPOSE_CMD = $(COMPOSE) $(if $(wildcard .env),--env-file .env) --env-file $(FRONT_DOOR_SECRETS) --profile gateway
GATEWAY_COMPOSE = $(GATEWAY_ENV) $(GATEWAY_COMPOSE_CMD)

# Named explicitly: the included files bring targets of their own.
.DEFAULT_GOAL := help

# `##@ Title` in a make file starts a group; `target: ## text` is a line of it.
help:  ## Show this help, by topic
	@awk 'BEGIN {FS = ":.*## "} /^##@/ {printf "\n\033[1m%s\033[0m\n", substr($$0, 5)} /^[a-zA-Z0-9_-]+:.*## / {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}' $(MAKEFILE_LIST)

include make/stack.mk
include make/data.mk
include make/host.mk
include make/gateway.mk
include make/droplet.mk
include make/test.mk
include make/evals.mk
include make/security.mk
include make/admin.mk

# Every rule here is a command, not a file. Computed, so a new target cannot be left out (a hand-kept list had already lost thirteen).
.PHONY: $(sort $(shell grep -hoE '^[a-zA-Z0-9_-]+:' $(MAKEFILE_LIST) | tr -d :))
