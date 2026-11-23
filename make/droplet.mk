# The dev stack on ONE DigitalOcean droplet (deploy/terraform/dev; deploy/README.md, "The dev droplet"): the repo is copied there, `make up-all` runs there with the hosted-LLM
# overlay, and the stack is reached through an SSH tunnel. Included by the Makefile.

# user@host, from `terraform -chdir=deploy/terraform/dev output -raw droplet`.
DEV_DROPLET ?=
DROPLET_DIR := /opt/graph-agent-core-dev
DROPLET_SSH  = ssh $(DEV_DROPLET)
# The same targets as on a laptop, with the hosted-LLM overlay (deploy/dev/hosted-llm.yml) added to the compose project.
DROPLET_MAKE = cd $(DROPLET_DIR) && make COMPOSE_OVERLAYS="-f deploy/dev/hosted-llm.yml"

##@ The dev droplet (one machine)

# Exactly the files git knows about (tracked, or new and not ignored), so a .env or any ignored secret cannot travel; the droplet's own .env is made there by hand
# (deploy/dev/droplet.env.example). rsync exit 24 is a file that vanished mid-copy, which a refactor in progress causes and which is not an error.
droplet-sync:  ## Copy the repo to the dev droplet, never a .env: DEV_DROPLET=deploy@<ip>
	@test -n "$(DEV_DROPLET)" || { echo "usage: make droplet-sync DEV_DROPLET=user@host   (terraform -chdir=deploy/terraform/dev output -raw droplet)"; exit 2; }
	@git ls-files -z --cached --others --exclude-standard | rsync -az --from0 --files-from=- ./ $(DEV_DROPLET):$(DROPLET_DIR)/ ; rc=$$?; [ $$rc -eq 0 ] || [ $$rc -eq 24 ]

droplet-up: droplet-sync  ## Build and start the whole dev stack there with the hosted LLM; the first time, then `make droplet-bootstrap`
	$(DROPLET_SSH) '$(DROPLET_MAKE) up-all'

droplet-bootstrap:  ## First time: create the Casdoor tenants and demo users on the droplet
	$(DROPLET_SSH) '$(DROPLET_MAKE) mcp-gateway-bootstrap'

droplet-ingest:  ## Load the sample documents and the skills catalog into the droplet's Qdrant (inside the api container: the droplet has no venv)
	$(DROPLET_SSH) 'cd $(DROPLET_DIR) && docker compose --project-directory . -f deploy/dev/docker-compose.yml -f deploy/dev/hosted-llm.yml exec -T api sh -c "python -m scripts.tools.seed && python -m scripts.tools.index_skills"'

droplet-ca:  ## Fetch the droplet's Caddy root certificate so this machine can trust https://*.localhost:8443 through the tunnel
	$(DROPLET_SSH) '$(DROPLET_MAKE) mcp-gateway-ca'
	@mkdir -p $(GATEWAY_DIR) && scp -q $(DEV_DROPLET):.cache/graph-agent-core-mcp-gateway/caddy-root.crt $(GATEWAY_DIR)/caddy-droplet-root.crt
	@echo "trust $(GATEWAY_DIR)/caddy-droplet-root.crt in your browser or OS; for Claude Code: export NODE_EXTRA_CA_CERTS=$(GATEWAY_DIR)/caddy-droplet-root.crt"

droplet-tunnel:  ## Forward the front door (https://api.localhost:8443/), Grafana :3300, Langfuse :3000, LiteLLM :4000 and Open WebUI :8080 here; Ctrl-C stops it
	$(DROPLET_SSH) -N -L 8443:localhost:8443 -L 3300:localhost:3300 -L 3000:localhost:3000 -L 4000:localhost:4000 -L 8080:localhost:8080

droplet-ssh:  ## Open a shell on the dev droplet
	$(DROPLET_SSH)

droplet-down:  ## Stop every container there, volumes kept (`terraform destroy` in deploy/terraform/dev removes the droplet)
	$(DROPLET_SSH) '$(DROPLET_MAKE) down-all'
