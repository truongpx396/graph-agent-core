# Deploying to a Digital Ocean droplet

Provisions TWO droplets (via `deploy/terraform/prod/`; the dev environment on one droplet is `deploy/terraform/dev/`, see [The dev droplet](#the-dev-droplet)):

- The **app droplet**, running a lean production subset of this app's stack
  (`deploy/prod/`): api + agent-worker + ingest-worker + postgres
  + redis + qdrant + litellm + ml-service, fronted by the same front door as
  the development stack (Caddy on three real names, `authlimit`, Casdoor and
  the MCP gateway; the Telegram bot is opt-in), plus a handful
  of lightweight observability sidecars (node-exporter, cadvisor,
  otel-collector-agent, promtail). It deliberately excludes local Ollama,
  Langfuse, open-webui, and MinIO from the development stack (`deploy/dev/`) — see
  `deploy/prod/docker-compose.yml`'s header comment for the tradeoffs and how to add any of them
  back.
- The **observability droplet**, a separate, smaller box running
  Prometheus/Loki/Grafana/Alertmanager/otel-collector
  (`deploy/prod/observability.yml`), fed by the app droplet's
  sidecars above over the DO VPC's private network — never the public
  internet. Kept off the app droplet so a metrics/logs spike can't compete
  with it for CPU/mem. Grafana is the only thing exposed publicly (via its
  own Caddy, real password); Prometheus/Loki/Alertmanager stay private.

Two separate, deliberately non-automatic-together pieces:

- **Infra (Terraform)** — creating/resizing/destroying the droplet itself.
  Always a human running `terraform plan`/`apply` by hand (below), against
  the local state file `versions.tf`'s backend block defaults to. CI's
  `config` job checks `fmt`/`validate` and `security.yml` runs Checkov —
  structural correctness only, not a real `plan` diff, since a real plan
  needs the actual state file, which a local backend deliberately keeps off
  CI's machine. If you switch to the commented-out DigitalOcean Spaces
  backend in `versions.tf` (recommended for a team), a real `terraform
  plan` step becomes meaningful in CI too and can be added the same way.
- **App deploys (`.github/workflows/deploy.yml`)** — pushing new code to an
  *already-provisioned* droplet. CI builds, scans and publishes the images and
  calls it as its last job on `main`; it SSHes in and runs
  `prod/release.sh`. Off until the `DEPLOY_ENABLED` variable is `true`.

## Layout

One directory per environment, with the same file names, so what is changed in one is looked for in the other:

```
deploy/
  dev/                       the local stack (`make up`)
  prod/                      the two droplets
    docker-compose.yml         the entry: names the parts it `include`s and nothing else
    infra.yml                  Postgres, PgBouncer, Redis, Qdrant, LiteLLM (dev adds MinIO, Langfuse, Open WebUI; prod has a second Redis, the answer cache)
    app.yml                    what this repo builds: the API, the workers, the ML service, the MCP gateway (prod adds the Telegram bot)
    gateway.yml                the front door: Caddy, authlimit, Casdoor
    collectors.yml             exporters and agents that feed the observability backends (a part of the project in prod; in dev an overlay of the three database exporters)
    observability.yml          a project of its own: Prometheus, Loki, Grafana, Alertmanager, Pyroscope (dev also holds the OTel collector, Promtail, node-exporter, cAdvisor and SonarQube, profile `quality`)
    litellm-config.yaml        the gateway's model list
    caddy/Caddyfile            how each site is addressed and certified (prod also has Caddyfile.observability)
    tools.yml                  what the agent's tools run on: the crawler (dev: part of the project, with the OpenSandbox server; prod: an opt-in overlay, see The crawler's network)
    dev only:  hosted-llm.yml (an overlay: LiteLLM on a hosted endpoint, for the dev droplet), droplet.env.example, loadtest.yml, litellm-patches/
    prod only: backup.sh, compose.sh, crawler-firewall.sh, prod.env.example, observability.env.example
  shared/caddy/gateway.caddyfile the front door's routing, imported by both Caddyfiles: change it once (pattern 98)
  images/                    the sandbox image builds
  terraform/                 one root per environment over a shared cloud-init.tpl.yaml
    prod/                      the two droplets (app and observability)
    dev/                       one droplet that runs the whole dev stack (see The dev droplet)
```

Every path in these files, the `include` paths too, is relative to the repository root, so compose runs with `--project-directory .` (the Makefile's `COMPOSE` and every alias below do);
without it the first `include` fails with "no such file", where a mistake used to take the wrong project name and orphan the volumes. `.env` and any extra `--env-file` reach every part.
Each Caddyfile imports the routing by a path relative to itself (`../../shared/caddy/gateway.caddyfile`) and the Caddy container mounts both directories where they sit in the repo, so the import resolves
in the container as on a checkout.

Two differences are on purpose. **The OpenSandbox server is never in production** (it mounts the Docker socket), so `OPENSANDBOX_MCP_DOMAIN` stays empty there and the sandbox tools are absent;
**the crawler is opt-in** there, as an overlay behind a host firewall ([The crawler's network](#the-crawlers-network)). **The Telegram bot** is a container in both environments
(`--profile telegram`; in dev `make telegram-up`), and in dev it can also run as a host process (`make telegram`).

## One-time setup

### 1. Prerequisites

- A DigitalOcean account and an [API token](https://cloud.digitalocean.com/account/api/tokens)
  (read + write).
- An SSH key uploaded to your DO account for your own (human) access:
  `doctl compute ssh-key create my-key --public-key-file ~/.ssh/id_ed25519.pub`,
  then `doctl compute ssh-key list` for its fingerprint.
- A **separate** key pair dedicated to CI deploys — don't reuse your
  personal key:
  ```
  ssh-keygen -t ed25519 -f ./deploy_key -C "graph-agent-core-ci" -N ""
  ```
  Keep `deploy_key` (private) and `deploy_key.pub` (public) handy for the
  next two steps.

### 2. Provision both droplets

```
cd deploy/terraform/prod
cp terraform.tfvars.example terraform.tfvars   # fill in do_token, ssh_key_fingerprints,
                                                # admin_ip_cidrs, deploy_ssh_public_key (deploy_key.pub's contents)
terraform init
terraform plan
terraform apply
```

This provisions both droplets in one apply. Note three outputs:
`reserved_ip` (app droplet's stable address), `observability_reserved_ip`
(observability droplet's stable address), and `observability_private_ipv4`
(its private VPC IP — needed in step 4 below).

### 3. Configure GitHub Actions

Settings -> Environments -> New environment `production`; under "Deployment branches and tags" allow only `main`. Add these as
**environment** secrets:

| Secret | Value |
|---|---|
| `DROPLET_HOST` | `terraform output reserved_ip` |
| `OBS_DROPLET_HOST` | `terraform output observability_reserved_ip` |
| `DEPLOY_SSH_KEY` | contents of `deploy_key` (the **private** half from step 1) — authorized on both droplets |
| `DEPLOY_KNOWN_HOSTS` | both droplets' host keys, one line each (below) |

The deploy never scans for host keys itself: whatever answers on that address would be trusted. Take them once, and check each against
the key the droplet reports from its own console (`ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`):

```
ssh-keyscan -t ed25519 <reserved_ip> <observability_reserved_ip>   # the value of DEPLOY_KNOWN_HOSTS
ssh-keyscan -t ed25519 <reserved_ip> <observability_reserved_ip> | ssh-keygen -lf -   # the fingerprints to compare
```

Then, when the droplets are ready for it, set the repository **variable** `DEPLOY_ENABLED` to `true` (Settings -> Secrets and variables ->
Actions -> Variables). Until then CI publishes images and deploys nothing. GHCR needs no secret: the workflows use their own `GITHUB_TOKEN`.

### 4. Create each droplet's `.env` (once, by hand)

The deploy workflow ships code and config, **never secrets** — it never
writes or touches `.env` on either droplet (see `deploy/prod/docker-compose.yml`'s
own header). SSH into each once and create it yourself:

```
ssh deploy@<reserved_ip>
sudo mkdir -p /opt/graph-agent-core   # cloud-init already does this; harmless if it exists
cd /opt/graph-agent-core
# paste this repo's deploy/prod/prod.env.example content into .env and fill in real
# values (POSTGRES_PASSWORD, LITELLM_MASTER_KEY, LLM_API_KEY, MINIO_*,
# CORS_ALLOWED_ORIGINS, APP_IMAGE/ML_IMAGE/AUTHLIMIT_IMAGE, API_DOMAIN/AUTH_DOMAIN/
# MCP_DOMAIN, CASDOOR_DB_PASSWORD/CASDOOR_ADMIN_PASSWORD, OBS_COLLECTOR_ENDPOINT/
# LOKI_PUSH_HOST from `terraform output observability_private_ipv4`, ...)
# — see that file's own comments.
nano .env
chmod 600 .env
```

```
ssh deploy@<observability_reserved_ip>
sudo mkdir -p /opt/agent-core-observability
cd /opt/agent-core-observability
# paste this repo's deploy/prod/observability.env.example content into .env and
# fill in GRAFANA_ADMIN_PASSWORD and ALERTMANAGER_SLACK_WEBHOOK_URL (REQUIRED: the stack
# refuses to start without it, since without a receiver no alert is ever delivered), and
# OBS_DOMAIN_NAME if you're pointing a subdomain at it — see that file's own comments.
nano .env
chmod 600 .env
```

### 5. First deploy

**Before it, do [step 7](#7-point-dns-at-them-required)** (DNS for the three names: Caddy asks Let's Encrypt for a certificate for each on first
use, and a name that does not resolve yet is retried with backoff and counts against Let's Encrypt's failure limits). **After it, do
[steps 8 and 9](#8-create-authlimits-signing-keys-once)**: until the signing keys exist `authlimit` restarts in a loop and every route that needs
the front door answers 502 (it fails closed); `/health` and the web page stay up, so the deploy's own readiness check passes.

Merge to `main`. In that commit's `CI` run:

1. `image` builds each of the three images, checks it, scans it and pushes it to GHCR as `:<sha>`.
2. `promote`, once every job has passed and the commit is still main's newest, tags those three `:latest`.
3. `deploy` calls [`deploy.yml`](../.github/workflows/deploy.yml), only when `DEPLOY_ENABLED` is `true`. Its `app` job syncs the
   compose and config files of that commit and runs [`prod/release.sh <sha>`](prod/release.sh) on the app droplet: pull, check the
   billing schema (only when `CREDITS_PER_USD` is set in `.env`), `up -d`, record the tag in `.image-tag`, wait for `/health/ready`
   through the front door, then remove images unused for a week. Its `observability` job does the same sync and `up -d` for the
   monitoring droplet.

Watch it in the Actions tab, or SSH in and use the `dc` alias from [Everyday operations](#everyday-operations) below: `compose.sh` reads
`.image-tag`, so a manual `dc up -d` keeps the release that is running.

**Moving an existing droplet to `deploy/prod/compose.sh`.** The deploy workflow now runs compose through `deploy/prod/compose.sh`, and ships it with `tools.yml` and `crawler-firewall.sh`. With
`CRAWLER_ENABLED` unset it runs exactly the command the workflow ran before, so nothing changes until you opt in. Two things on the droplet are yours to change by hand: your shell's `dc` alias, and any
cron line that calls `docker compose` directly (those keep working, but would not know about the crawler).

**Moving an existing droplet to the `deploy/prod/` layout.** The first deploy after this layout lands changes the config of `caddy` and `litellm` on the app droplet (their mount sources moved: a few seconds
of TLS and gateway blip) and of `caddy` on the observability droplet; `api`, `agent-worker` and `ingest-worker` are recreated as on every deploy (compared with `docker compose config --hash` before and after).
The project names, so the volumes, do not change. Three things are yours to do, because the workflow cannot know them:

1. **The backup cron line.** `backup.sh` is not shipped by the workflow; it is on the droplet from your clone. Bring `deploy/prod/backup.sh` over (`git pull`, or copy it) and change the crontab to
   `/opt/graph-agent-core/deploy/prod/backup.sh`. Until you do, the old line keeps running the old script, which still works because the workflow leaves the old compose file where it is.
2. **Then remove the old files** from the app droplet: `rm -rf deploy/compose deploy/litellm deploy/env deploy/backup deploy/caddy/Caddyfile deploy/caddy/Caddyfile.gateway deploy/caddy/gateway.caddy`.
   Not before step 1: the old `backup.sh` calls `deploy/compose/docker-compose.prod.yml`, and deleting it would stop the backups without a sound. The deploy no longer removes old files itself:
   a droplet from before `deploy/` also has the flat copies `docker-compose.prod.yml`, `Caddyfile` and `litellm-config.prod.yaml` (the observability droplet: `docker-compose.observability.prod.yml`
   and `Caddyfile.observability`). Remove those too: a stale compose file left there still runs.
   A droplet that already ran the layout above also has `deploy/shared/caddy/gateway.caddy`, from before the routing file became `gateway.caddyfile` (so an editor highlights it). The deploy ships the new name and Caddy imports it,
   so the old file is dead weight: `rm deploy/shared/caddy/gateway.caddy` once a deploy has finished.
3. **Spend limits that now take effect.** `MAX_COST_USD_PER_TENANT_PER_MONTH`, the two `MAX_COST_USD_PER_PRINCIPAL_*` ceilings and `UNPRICED_MODEL_POLICY` were in `prod.env.example` but never
   passed to a container, so they did nothing. They are passed now (unset means off and `allow`, as before). A droplet whose `.env` already lists them, copied from the example, starts enforcing them
   at this deploy: read the values first, and check that LiteLLM prices your model before `UNPRICED_MODEL_POLICY=block` refuses an unpriced one.

**Upgrading an already-provisioned app droplet**: `api` no longer publishes
a fixed host port 8000 (Caddy now load-balances across replicas via a
`dynamic a` upstream instead — see `deploy/prod/caddy/Caddyfile`). `terraform apply` closes
port 8000 at the DO cloud firewall immediately either way (that's the first
enforcement layer), but cloud-init's matching `ufw` rule only applies on
first boot (see `cloud-init.tpl.yaml`'s own comment) — on a droplet
provisioned before this change, either taint+recreate it, or just run `ssh
deploy@<reserved_ip> sudo ufw delete allow 8000/tcp` once by hand; the
latter touches no app data.

**Profiling (specs/022)**: the observability droplet now runs Pyroscope, which the app droplet's api and workers push to on port 4040. The
cloud firewall rule is in `main.tf` (scoped to the VPC, like 4318 and 3100) and `ufw_tcp_ports` lists the port for a droplet provisioned
from now on. On an observability droplet that already exists, open it by hand (`ssh deploy@<observability_ip> sudo ufw allow from
<vpc_cidr> to any port 4040 proto tcp`), run `terraform plan` before applying (an edited `user_data` may be shown as a replacement; this
has not been planned against real state), then set `PYROSCOPE_SERVER_ADDRESS=http://<observability_private_ipv4>:4040` in the app droplet's
`.env` and redeploy. It is empty by default: profiling stays off until you do.

### 6. Mint the app's gateway key

Until you do this the app sends LiteLLM's **master key**, which is gateway admin and cannot carry a
budget, so the gateway has no spend cap of its own. Once the stack is up, mint a scoped,
budget-capped key and give it to the app. The script ships in the `api` image and reaches `litellm`
over the compose network; the master key is passed from your shell's environment, never as an argument:

```
cd /opt/graph-agent-core
export LITELLM_MASTER_KEY="$(grep '^LITELLM_MASTER_KEY=' .env | cut -d= -f2-)"
dc exec -e LITELLM_MASTER_KEY api python -m scripts.tools.litellm_key create --max-budget <usd per window> --rpm-limit 600
unset LITELLM_MASTER_KEY
nano .env          # paste the printed key as LITELLM_APP_KEY=
dc up -d           # recreates api / agent-worker / ingest-worker with the new key
```

- The key is printed **once**. `--max-budget` has no default because it is a business number: set it
  **above** the sum of your tenants' own monthly caps, so it fires only when the app-level limits have
  failed (below normal use it turns routine traffic into an outage). It covers chat and embeddings.
- Check it any time with `dc exec -e LITELLM_MASTER_KEY -e LITELLM_APP_KEY api python -m
  scripts.tools.litellm_key info` (spend, budget and the real reset time; LiteLLM aligned `30d` to the next
  month boundary when this was tried, so read `resets at` rather than assuming 30 days).
- When the key is spent, turns fail with `provider_budget_exceeded` and `GatewayBudgetExceeded` pages.
  Find who spent it in LiteLLM's spend logs (grouped by `end_user`, or the `tenant:<name>` tag) before
  raising the budget; `python -m scripts.tools.litellm_key end-user --tenant <name>` maps a name to its id.
- To roll the key, mint a new one, swap `LITELLM_APP_KEY`, `dc up -d`, then delete the old key in
  LiteLLM's UI.
- A key lists the aliases it may call. `create` now defaults to every alias the prod config defines
  (`chat`, `chat-medium`, `chat-strong`, `embed`); a key minted before that lists only `chat,embed` and
  LiteLLM will refuse `chat-strong` or `chat-medium` for it ("key not allowed to access model"). Re-mint
  it, or widen it with LiteLLM's `/key/update`, before turning the router on or attaching a tier.

**The per-person cap, and who a person is.** The app key's budget is one number for the whole app. The
app also sends every call with `user` = `tenant:<tenant>/user:<principal>` (for example
`tenant:acme/user:alice`; a `:` or `/` inside a name is written `%3A` / `%2F`), which LiteLLM keeps as the
call's *end user*: it is what the **Customer Usage** view in the admin UI filters by, so spend can be read
per person (`python -m scripts.tools.litellm_key end-user --tenant acme --principal alice` prints exactly what
the app sends). LiteLLM also holds a *default budget for every end user*, i.e. every person: the configs
name it (`litellm_settings.max_end_user_budget_id: person-default`) and **you create it once**; until you
do, LiteLLM logs "Default end user budget not found in database" on each call and applies no cap at all.

```
dc exec -e LITELLM_MASTER_KEY api python -m scripts.tools.litellm_key person-budget set --max-budget <usd per person per window>
dc exec -e LITELLM_MASTER_KEY api python -m scripts.tools.litellm_key person-budget info   # exits 1 if it is missing: run it after every deploy
```

- It caps **each person**, not a tenant: a tenant with many people can spend many times it, and a tenant's own
  limits are the app's (`MAX_COST_USD_PER_TENANT_*`, `budget_policies`). There is **no tenant-wide cap at the
  gateway**; that would need the tenant to be the end user, or a key per tenant.
- Size it **above the largest single person's legitimate spend** for the same window, like the key budget: it
  is a backstop beneath the app's limits (`MAX_COST_USD_PER_PRINCIPAL_*` are off by default), not a copy of
  them. `set` again changes it; LiteLLM reads it through a 60-second cache.
- It covers **chat calls only**: embeddings carry no end user yet. A person who reaches it gets the same
  `provider_budget_exceeded` and `GatewayBudgetExceeded` page as the key does; the spend logs' `end_user`
  says whose it was. A different number for one person is a LiteLLM customer with its own budget
  (`/customer/new`, `user_id` = the id above), which duplicates the app's `budget_policies`: prefer those.
- **The end user is readable on purpose, so it is only safe while LiteLLM does not forward `user` to a model
  provider.** Checked against the pinned image (nothing reached a stub backend, on the Ollama and the
  OpenAI-compatible routes); re-run that check whenever the image is bumped (below). If you created the
  earlier `tenant-default` budget, it is no longer referenced: delete it in LiteLLM's UI or with `/budget/delete`.

**Bumping the LiteLLM image.** Both compose files pin `ghcr.io/berriai/litellm:main-stable` by digest, and the two must
stay equal. To move it deliberately:

```
docker pull ghcr.io/berriai/litellm:main-stable
docker image inspect ghcr.io/berriai/litellm:main-stable --format '{{index .RepoDigests 0}}'
docker manifest inspect ghcr.io/berriai/litellm@sha256:<digest>   # must list linux/amd64 AND linux/arm64
```

Put the digest in both `deploy/dev/infra.yml` and `deploy/prod/infra.yml` (a test keeps the two equal), then run **`make gateway-check`** (the new
image on the shipped dev config and a stub model backend; Docker only, about a minute) and `make
probe-tools`, and check a budget stop by hand before shipping. `gateway-check` fails if an alias is
unpriced, if the gateway's cost is not tokens x the configured price, or **if the end user, tenant,
principal, conversation or metadata reaches the model backend**: the end user is readable on purpose, which
is only safe while LiteLLM keeps it to itself, and that is a property of the image, not of this repo. The
stop's HTTP status changed between releases once, and the app's checks were written against 1.94.1.

### 7. Point DNS at them (required)

Create an A (and AAAA, if you use one) record for each of `API_DOMAIN`, `AUTH_DOMAIN` and `MCP_DOMAIN` in the app droplet's `.env`, all pointing at the
`reserved_ip` output (they share one droplet and one Caddy). A domain is **required** for the app droplet: identity needs an https issuer, so there
is no plain-HTTP fallback, and `docker compose` refuses to start without the three names. Caddy requests a real certificate for each automatically on
first use; ports 80 and 443 are already open (`main.tf`'s firewall).

Likewise, if you set `OBS_DOMAIN_NAME` in the observability droplet's `.env`, point another record at `observability_reserved_ip` (that one is still
optional: see `deploy/prod/caddy/Caddyfile.observability` for its no-domain HTTP-only fallback).

### 8. Create authlimit's signing keys (once)

`authlimit` signs a 30-second identity for the API with an Ed25519 key kept in a host directory outside the repo and the image, mode 700, owned by the
container's user (uid 65532). Make it once, after the first deploy has pulled the image (the `latest` tag is the one just pushed):

```
cd /opt/graph-agent-core
image="$(grep '^AUTHLIMIT_IMAGE=' .env | cut -d= -f2-)"
sudo install -d -m 700 -o 65532 -g 65532 /opt/graph-agent-core/authlimit-keys
docker run --rm --user 65532:65532 -v /opt/graph-agent-core/authlimit-keys:/keys "$image:latest" keys init --dir /keys
dc restart authlimit
dc ps authlimit            # both replicas healthy
```

`keys rotate --overlap 10m` (the same `docker run`, with `keys rotate`) adds a new key, publishes it, and retires the old one after the overlap; the
replicas and the API pick it up without a restart. The directory is a secret: back it up with the `.env`, and never put it in the repo or an image.
If it is lost, run `keys init` again: every replica and the API fetch the new public key, and requests in flight with the old identity (30 seconds) fail once.

### 9. Create the tenants and sign-in clients (once)

Casdoor starts empty. `front_door_bootstrap` creates, idempotently, one Casdoor organization per tenant, the chat API's public sign-in client for it
(`api-<tenant>`, which the web page, the CLI and the Telegram bot look up by that name), the MCP applications, and one demo user per organization. It runs
inside the MCP gateway container, where Casdoor is `http://casdoor:8000`, and needs the admin password from your shell's environment, never as an argument:

```
cd /opt/graph-agent-core
export CASDOOR_ADMIN_PASSWORD="$(grep '^CASDOOR_ADMIN_PASSWORD=' .env | cut -d= -f2-)"
dc exec -T -e CASDOOR_ADMIN_PASSWORD mcp-gateway python -m scripts.tools.front_door_bootstrap --tenant ecorp --token-hours 24 \
  [--tenant acme ...] [--telegram-link-url https://<API_DOMAIN>]
unset CASDOOR_ADMIN_PASSWORD
```

- It prints each demo user's password **once**. Those are demonstration accounts: sign in to `https://<AUTH_DOMAIN>` as `built-in/admin`, create your
  real people, and disable the demo ones. A shorter `--token-hours` with a refresh token is the production recommendation (the default is seven
  days; `authlimit revoke` voids a person's tokens at once, see the keys image's `revoke` command).
- `--telegram-link-url` registers the bot's `/telegram/link/callback` as a redirect URI of every tenant's client; leave it out unless the bot links accounts.
- **The admin UI shares the sign-in host.** Anyone can reach `https://<AUTH_DOMAIN>/login`, so the admin password (required, generated) is what protects every
  tenant. Narrowing it by source address is not done: Caddy would need Casdoor's admin paths, which this repo does not enumerate. The sign-in routes are
  charged per address by authlimit (`routes.json` `address_limits`), which slows guessing; it does not stop it, and it does not stop an attacker locking
  an account (Casdoor locks one after five wrong passwords).

**The Telegram bot** is opt-in: set its variables in `.env` (see `prod.env.example`), then `dc --profile telegram up -d telegram-bot`. (Dev has the same container: `make telegram-up`.) In `poll` mode (the default) it
needs nothing public; in `webhook` mode Caddy routes `https://<API_DOMAIN>/telegram/webhook` to it, which Telegram calls with the secret you set.

## Everyday operations

```
ssh deploy@<reserved_ip>
cd /opt/graph-agent-core
# The compose files live under deploy/prod/ but every path in them (the `include`s
# too) and .env are relative to this directory, so every call needs --project-directory .
# (an env var can't replace it: COMPOSE_FILE alone makes paths resolve under deploy/prod/).
# compose.sh is that command, plus the crawler overlay when .env says CRAWLER_ENABLED=true; CI's deploy goes through it too.
alias dc='deploy/prod/compose.sh'
dc ps
dc logs -f api
dc logs -f authlimit caddy         # the front door: a 429 is a rate limit, a 401 a token, a 502 on a checked route authlimit not ready
dc up -d --scale agent-worker=3   # scale workers (GRAPH_PATTERNS.md pattern 43)
```

Observability droplet, separately:

```
ssh deploy@<observability_reserved_ip>
cd /opt/agent-core-observability
alias dc='docker compose --project-directory . -f deploy/prod/observability.yml'
dc ps
dc logs -f grafana
```

Grafana is at `https://<obs_domain>` (or `http://<observability_reserved_ip>`
with no domain set) — log in with `admin` / `GRAFANA_ADMIN_PASSWORD`. If a
dashboard shows no data, check the app droplet's relay first:
`dc logs otel-collector-agent promtail` on the app droplet — both should show successful pushes to the observability
droplet, not connection errors.

**Backups**: see "Backups and restore" below (a script, a cron line, a restore that was performed). `enable_backups` (terraform.tfvars) additionally turns on DO's weekly
whole-droplet image backups for the **app** droplet only, off by default. The observability droplet is never backed up: it holds metrics and logs with their own
bounded retention (Loki 7d, Prometheus 15d), not data worth restoring.

**Vector payload indexes (once per existing deployment)**: every search filters by `tenant`, `kind`, `owner`, `topic` and `created_at`, and a collection
built before the payload indexes existed has none, so its filtered search slows as it grows. `make ingest` and `make reembed` build indexed collections;
for the one you already have, run `make qdrant-indexes` once (`ssh` in, then `dc run --rm api python -m scripts.tools.qdrant_indexes`, or from a checkout
that reaches Qdrant). It is online, safe to repeat, and reads and writes no point.

**Changing the embedding model**: the model, its size, either prefix and `EMBED_MODEL_REVISION` are one identity, and a collection is only read by an app
configured for the identity it was built under (pattern 67). **Do not point the `embed` alias at another model**: the app notices within a minute and refuses
retrieval until every collection has been rebuilt. Add a second alias and move to it:

1. Judge the candidate first, on a development machine: `make model-eval KIND=embedding CANDIDATE=<name>` (`evals/README.md`).
2. Add it to `deploy/prod/litellm-config.yaml` beside `embed` (a `model_name: embed-v2` entry of the same shape), release, restart the proxy
   (`dc restart litellm`; `up` does not recreate it for an edited file) and let the app's key call it (LiteLLM's `/key/update`; a key lists the
   aliases it may use).
3. Stop ingestion (`dc stop ingest-worker`) and pick a quiet hour. Chat still writes memories and notes: a write during the rebuild stops it with exit 4 and
   nothing swapped, so run it again.
4. Rebuild beside the live collections, with the new settings given to this one command only, so the running app keeps answering from the old ones. Add a
   `-e` for each prefix or revision that changes too. Each command swaps its collection when it finishes, and the app refuses that collection until step 5,
   so go straight on:
   ```bash
   dc run --rm -e EMBED_MODEL=embed-v2 api python -m scripts.tools.reembed docs     # the long one
   dc run --rm -e EMBED_MODEL=embed-v2 api python -m scripts.tools.reembed skills
   ```
5. Put the same settings in `.env` (`EMBED_MODEL=embed-v2`) and `dc up -d`, which also starts ingestion again.
6. Check: `dc run --rm api python -m scripts.tools.vector_collections` shows each live collection `IN USE`, and `/health/ready` lists no identity warning.
7. After a bake period, delete the collections it shows as `not in use`:
   `dc run --rm api python -c "import httpx; httpx.delete('http://qdrant:6333/collections/<name>').raise_for_status()"`.

To go back, run steps 3 to 6 with the old settings: it rebuilds from the live collection, so nothing written since is lost, and takes as long as the first run.
What this does **not** give, and Qdrant's [migration tutorial](https://qdrant.tech/documentation/tutorials-operations/embedding-model-migration/) does: writes
go to one collection only, retrieval is refused from the swap to the restart, and there is no instant way back.

**A hung worker**: the agent and ingest workers bind no port, so each touches a heartbeat file from its read loop and its reclaim loop
(`app/platform/worker_heartbeat.py`) and the compose health check fails when one is more than two minutes old (a stuck event loop, a
deadlock). `dc ps` then shows the worker `unhealthy` and the alert `AgentWorkerLoopStalled` fires when no replica of that queue has
looped for three minutes. **Docker Compose does not restart an unhealthy container**, and neither does `restart: unless-stopped` (it
restarts one that *exits*). What does: an orchestrator that acts on health, or a sidecar such as `willfarrell/autoheal` (it needs the
Docker socket, which is a trade-off this repo has not made for you), or `dc restart agent-worker` by hand when the alert fires. A worker
that is merely busy is not stale: it beats while it waits for a free slot.

**Prompts in the gateway's spend log**: off in production (`store_prompts_in_spend_logs: false` in
`deploy/prod/litellm-config.yaml`), so the gateway's database holds metadata (model, tokens, cost, end user) and not what a person
asked or what was answered. To see request and response bodies for a period (debugging a guardrail, say), set it to `true` **and** add
`maximum_spend_logs_retention_period: "7d"` under `general_settings` beside it, restart the proxy, and turn both back off afterwards. The key
is the one in litellm 1.94.1's `proxy/_types.py`; check it against the image you have pinned before relying on it. Rows written while it was
on are not removed by turning it off.

**Rollback**: Actions -> Deploy -> Run workflow on `main`, with `image_tag` = the full SHA of an earlier commit on main. It syncs that
commit's compose files and releases that commit's images, so both go back together. Without GitHub, on the droplet:
`deploy/prod/release.sh <sha>` (images only; it needs `docker login ghcr.io` with a token that can read packages). A failed release prints
the previous SHA. Database schema changes are not undone by either. The observability images are pinned in
`deploy/prod/observability.yml` and `collectors.yml`: bump them there.

**Destroying a droplet**: `cd deploy/terraform/prod && terraform destroy` removes
**both** droplets and both reserved IPs in one go. To remove just the
observability droplet and keep the app running, target it specifically:
`terraform destroy -target=digitalocean_droplet.observability
-target=digitalocean_reserved_ip.observability`. Either way, data in the
app droplet's Docker volumes (postgres/qdrant/redis) is gone with it unless
backed up first (see above); the observability droplet has nothing worth
preserving.

## Upgrading: carry the ledger's history into the usage events

The dollar caps and `GET /usage` now sum `usage_events` (one row per model call) instead of `usage_ledger` (one row per turn), specs/010 T030b. This applies to **every** deployment, with or without
credit billing. The events only have rows from the day `postgres-init/19-usage-events.sql` was applied, so do it in this order:

1. Apply `postgres-init/19-usage-events.sql` on an existing volume. Skipping it makes every cap read fail:
   under the default `BUDGET_CHECK_FAILURE_POLICY=open` every turn then runs **unchecked** and `TenantAllowanceUnenforced` pages; under `closed` every turn is refused.
2. `make usage-events-carry-over ARGS=--dry-run` and read what it says, then `make usage-events-carry-over`. It copies each `usage_ledger` row older than the first real usage
   event into `usage_events` as `ledger:<id>` (never rated, charged or exported; idempotent; a run that stops at its ceiling is continued by the next), so a monthly cap does not forget the month so far. `ARGS="--tenant acme"` does one tenant.
3. Deploy the release.
4. Run `make usage-events-carry-over` once more. It normally prints `Nothing to carry`; if it carries rows, they were recorded in the gap between steps 2 and 3.

Nothing alerts that step 2 was skipped, so check it: `ARGS=--dry-run` printing `Would carry 0` means the history is whole. There is **no way back by reverting**: since T030c2 nothing writes
the per-turn ledger, so a release that reads it would know spend only up to the day this one deployed. `USAGE_EVENTS_ENABLED=false` no longer exists as a switch (it is refused at startup): the caps would read $0.

## Reconciling the agent's meter with the gateway

The agent compares its own usage events with the LLM gateway's spend log, the one independent record of what was really spent. A third party's meter is the only thing that
notices a call whose event was never written.

```
make usage-reconcile                        # the agent's events against the gateway: 0 agree, 1 drift, 2 could not finish
make usage-reconcile ARGS="--days 7"        # look further back (1-35)
make usage-reconcile ARGS="--json"          # machine-readable
make usage-reconcile-worker                 # a pass every USAGE_RECONCILE_INTERVAL_SECONDS, feeding the gauge and alert
```

The gateway comparison needs the gateway's **admin** key (spend logs are an admin view): `LITELLM_MASTER_KEY` in the environment, never an
argument, and `LITELLM_URL` unless the app's own gateway address resolves (`http://litellm:4000` on the app droplet). Give it to the
`usage-reconcile` container only; the app itself should keep running on `LITELLM_APP_KEY`. The worker refuses to start without the key rather than
quietly skipping the independent meter.

No worker is a compose service yet (disclosed): on the app droplet run them from the app image, which carries `scripts/` and the billing package:

```
# LITELLM_MASTER_KEY is in the droplet's .env; compose reads .env for interpolation only, so export it for this one command
set -a; . ./.env; set +a
dc run -d --no-deps --name usage-reconcile  -e LITELLM_MASTER_KEY api python -m scripts.jobs.usage_reconcile --loop
dc run -d --no-deps --name credit-reconcile api python -m credit_billing.cli.reconcile --loop      # credit billing only
dc run -d --no-deps --name billing-export   api python -m credit_billing.cli.export_worker         # only if a provider bills on usage
```

**Reading a finding.** Each names the tenant, the UTC day and the amounts, and says what it means:

| Finding | Meaning | First step |
|---|---|---|
| `gateway`, drift **above** zero | the gateway spent more than the events record: a call whose event was never written (`UsageEventWriteFailing`), an unpriced call (the note says how many), or spend the app does not meter | check the alert history for that day; `usage_event_unpriced_total`; the pricing of the model. A call with no event is also a call no dollar cap counted |
| `gateway`, drift **below** zero | events with no call behind them, or spend the gateway lost | check the gateway database and its spend-log write queue |
| a tenant you do not recognise, with `gateway` drift above zero | the gateway spent for a tenant with no events at all, the worst shape a lost meter takes (the tenant is read off the end user `tenant:<tenant>/user:<principal>`, so it is named) | check it is not another deployment sharing the gateway (its end users that are not this app's format go to `unattributed`, never here) |
| a tenant named `tenant_<hash> (no tenant in this database hashes to it)` | the same, in a spend-log row written before the end user named the person (the older one-way id) | look the hash up with `python -m scripts.tools.litellm_key end-user --tenant <name>`; these age out with the gateway's spend logs |

**Tolerance.** A difference is drift only above the larger of `USAGE_RECONCILE_TOLERANCE_USD` and `_PCT` of the larger figure. A small
**persistent** gap on every tenant is a price mismatch between the app and the gateway for one of your models, not a loss: tune the percentage
to your own difference rather than ignoring the report. The newest `USAGE_RECONCILE_SETTLE_SECONDS` of traffic is left out (the gateway
writes its spend log in batches, so the newest calls are in the events first).

| Alert | Means | Do |
|---|---|---|
| `UsageReconcileDrift` | the gateway and the agent's events disagree above tolerance on some tenant-day | `make usage-reconcile` |
| `UsageReconcileNotCompleting` | a gateway pass failed, or the gateway had more rows than `USAGE_RECONCILE_GATEWAY_MAX_PAGES` | log `usage_reconcile_failed`; shorten `USAGE_RECONCILE_LOOKBACK_DAYS` or raise the ceiling |
| `UsageEventWriteFailing` | a call was made and its event was not written | reconcile that day against the gateway |

Disclosed gaps: the gauge exists only while `usage-reconcile-worker` runs (a stopped worker **resolves** `UsageReconcileDrift`; cron plus the `make` command still gives the report and the exit code,
not a durable alert; `tests/core/test_gauge_export.py` pins the SDK behaviour this rests on). A pass scans `usage_events` by time while the table's index leads with the tenant (a sequential scan:
fine at moderate volume; a time-only index would tax every model call's insert, so `USAGE_RECONCILE_LOOKBACK_DAYS` is the lever). Embeddings are neither metered nor attributed to a tenant, so their
spend shows as "no tenant of this app", never as drift. A call across midnight UTC is recorded on one day by the gateway (its start) and another by the event (its insert), which shows as a pair of
opposite drifts on adjacent days: that signature is the straddle, not a loss.

## Trimming the usage events

**`make usage-events-sweep`** deletes the agent's usage events older than `USAGE_EVENT_RETENTION_DAYS` (400 by default, **floor 35**: a monthly cap and the reconciliation read back that far). This is the
table spend is read from. It takes no arguments; the window is the setting. It prints what it did and says when it stopped at its ceiling (run it again). Nothing schedules it: run it from cron once your retention
policy is decided. The billing package has its own sweep for its own table (`make billing-usage-sweep`, in the billing [runbook](../packages/billing/docs/RUNBOOK.md)).

## Backups and restore

**What this protects, stated plainly.** The production stack is one droplet with a single instance of each store (Postgres, Redis, Qdrant). Any one of them going down takes the service down, and a backup does not change that:
it is what lets you rebuild after losing one. The code is ready for more (stateless API and workers, PgBouncer, per-domain queues); moving the stores to managed services is an operations decision, not a code change, and would change this:

| | Today | With a managed service |
|---|---|---|
| availability | one instance each; a failure is an outage until you restore | a standby and automatic failover (Postgres), a replica (Redis) |
| backups | `deploy/prod/backup.sh`, daily, from this droplet | the provider's, with point-in-time recovery for Postgres; this script's Postgres half is then retired |
| what to change | | the connection strings only: `CHECKPOINTER_DATABASE_URL`, `APPDATA_DATABASE_URL` (through PgBouncer or not), `REDIS_URL`, `QDRANT_URL` |
| what to check first | | the managed Redis must offer streams, Lua and (for the answer cache) the search and JSON modules, which many do not; without them the cache needs another home or goes off (`SEMANTIC_CACHE_ENABLED=false`). It must also be set to `noeviction` for the queue (see "Redis" below) |

**The job.** `deploy/prod/backup.sh` takes, on the app droplet: `pg_dump --format=custom` of `appdata`, `checkpointer` and `litellm` (inside the `postgres` container) and of Casdoor's `casdoor` (inside `casdoor-db`); a snapshot of every Qdrant collection, with the aliases
(`scripts/tools/qdrant_backup.py`, from the app image: Qdrant's has no `curl`); and the queue Redis (`redis-cli --rdb -`: the locks, the buckets and the front door's **revocation list**). It reads every dump back with `pg_restore --list`, copies the directory to the uploads bucket under `backups/<UTC stamp>/`
with a manifest of sizes and SHA-256 (`scripts/tools/backup_upload.py`), and keeps the newest `BACKUP_KEEP_LOCAL` (3) complete copies on the droplet. A directory is complete only once it has lost its `.partial` suffix, and a bucket copy only once its `manifest.json` exists; any failing step fails the run
and leaves no complete backup. Put it in cron as the deploy user, with the Spaces settings already in `.env`:

```
17 2 * * *  /opt/graph-agent-core/deploy/prod/backup.sh >> /var/log/graph-agent-backup.log 2>&1
```

**Not backed up, on purpose:** the answer cache (disposable); the observability droplet; the uploaded files themselves (they live in the bucket: set versioning or a lifecycle rule there, since this job writes beside them and never deletes); and the **secrets**: `.env`, `authlimit-keys/` and the Terraform state are not in any backup, so keep them in a password manager. Casdoor's own signing keys are in its database, which is backed up.
**Expire old backups** with a lifecycle rule on the `backups/` prefix in the Spaces console; nothing here deletes from the bucket.

**Nothing alerts when the backup stops running.** That is the failure a cron job never reports. From somewhere that is not the droplet (another cron, an uptime monitor), run `python -m scripts.tools.backup_upload --latest --max-age-hours 30` (from the app image): it exits 1 when there is no complete backup or the newest is older than the limit, and prints which one it found.

**RPO** is the cron interval: with the line above, up to a day of conversations, usage events (the spend caps read them, so a restore loses that day's spend and the caps allow it again), documents ingested and approvals paused is gone. Tighten the interval or move Postgres to a managed service if that is too much.

**Restoring onto a rebuilt droplet** (Terraform applied, the repository cloned, `.env` restored; this is the sequence `make restore-drill` performs, against fresh containers):

```
alias dc='deploy/prod/compose.sh'
dc up -d postgres casdoor-db redis redis-cache qdrant                       # the stores only: no api, no workers, nothing writing yet
mkdir -p /restore && dc run --rm --no-deps -T --user "$(id -u):$(id -g)" -v /restore:/backup api python -m scripts.tools.backup_upload --download backups/<stamp> --to /backup   # checks every file against its manifest
for db in appdata checkpointer litellm; do dc exec -T postgres pg_restore -U langfuse --clean --if-exists -d $db < /restore/$db.dump; done
dc exec -T casdoor-db pg_restore -U casdoor --clean --if-exists -d casdoor < /restore/casdoor.dump
dc run --rm --no-deps -T -v /restore:/backup:ro api python -m scripts.tools.qdrant_backup --restore /backup      # the collections, then the aliases
dc up -d                                                                    # the rest
```

`--clean --if-exists` drops and recreates each object, so it is safe over the empty schema the init scripts leave. Read `pg_restore`'s exit status: a non-zero one is a failed restore. `qdrant_backup --restore` refuses a collection that already exists (`--overwrite` replaces it).
Redis is the one that needs care: do **not** just copy `redis.rdb` into place, because with AOF on Redis ignores a snapshot and starts empty (ENGINEERING_PATTERNS.md 100). Either accept an empty Redis (the queue and locks rebuild themselves, and you re-apply revocations from your own record with `authlimit revoke`), or restore it as the drill does:
stop `redis`, put the file in its volume as `dump.rdb`, start it once with `REDIS_ARGS=--appendonly no` so it loads it, run `redis-cli config set appendonly yes`, wait until `info persistence` shows `aof_rewrite_in_progress:0`, then start it with its normal flags. Then check: `dc exec authlimit authlimit revoke --list`,
`curl https://<API_DOMAIN>/health/ready`, and one real turn.

**The drill, performed** (`make restore-drill`; `ARGS="--points 100000"` for sizes near yours). It starts throwaway containers from the shipped images (Postgres exactly as the production compose file defines it, on an empty volume initialised from `postgres-init/`), seeds them, runs the commands above, restores into a SECOND set of fresh containers and
checks: row counts and a checksum over the rows, the points and the same search through the restored alias, the payload indexes, and the Redis keys before and after AOF is turned on again. One run, on a developer laptop (macOS, Apple silicon, Docker Desktop with 12 CPUs and 14 GB), 2026-11-15:

| component | data | backup | restore | verified |
|---|---|---|---|---|
| `appdata` + `checkpointer` + `litellm` | 50,000 sessions, 5,000 checkpoints; dumps 0.5, 0.2 and 0.0 MB | 0.3 s | 0.7 s | counts and md5 of the rows |
| `casdoor` | 2,000 users | 0.1 s | 0.1 s | md5 of the rows |
| Qdrant | 20,000 points of 768 dimensions, a 141.7 MB snapshot, behind an alias | 2.4 s | 3.1 s | count, top-3 of a query, the payload indexes |
| Redis | 20,000 keys and a 20,000-entry stream, 0.7 MB | 4.8 s | 1.2 s | every key, before and after AOF is switched on again |
| total | | 7.6 s | 5.0 s | |

**Read those numbers for what they are.** They are this laptop's, on small data and a local disk: not a droplet's, and nothing like a production database. Another run of the same drill totalled 7.9 s and 4.4 s, so read them to the second, not the decimal. About 5 of the Redis seconds are Redis's own replication delay (`repl-diskless-sync-delay`), not size. They leave out everything around the restore: provisioning the droplet, pulling images, DNS and certificates, and the copy to and from the bucket
(a network cost that grows with the snapshot: Qdrant's is the large one). So they show the commands work and the data comes back identical, not how long a real recovery takes: **the end-to-end time to recover has not been measured.** Run the drill with your own sizes, and time the rest once on a spare droplet.

**What the drill found.** The production compose file set `POSTGRES_DB: appdata`, so on a NEW volume the Postgres image created `appdata` itself and then `postgres-init/02-appdata.sql` failed on `CREATE DATABASE appdata` ("already exists"): the container exited with status 3. An existing volume never runs the init again, which is why nothing showed it. A rebuilt droplet could not have started its database, so no restore was possible. It is now
`POSTGRES_DB: postgres`, and `tests/integration/test_production_postgres_initialises.py` starts the production service as defined and fails on the old value.

## Redis: the queue and the cache

Production runs **two** Redis services from one pinned image (`deploy/prod/infra.yml`; PRODUCTION_REVIEW.md R15). They cannot share a policy, because no eviction setting is right for both:

| | `redis` (the queue) | `redis-cache` (the answer cache) |
|---|---|---|
| holds | the work queue, the conversation locks, the cancel flags, the dedup claims, the front door's rate buckets **and revocation list**, the Telegram state | the semantic cache |
| persistence | `--appendonly yes --appendfsync everysec` (a restart loses about a second of accepted jobs; it used to lose whatever the last snapshot missed) | none: no snapshot, no AOF, no volume |
| when full | `--maxmemory-policy noeviction`: a write is **refused** (a new turn fails to publish and the caller sees an error); nothing is dropped | `allkeys-lru`: it evicts the least recently used, which is what a cache is for |
| `maxmemory` / `mem_limit` | 192mb / 256m | 128mb / 192m |

**The two sizes are yours to set** (the defaults suit the default droplet, `s-4vcpu-8gb`). Keep `maxmemory` at about 75% of `mem_limit` (the rest is for the AOF rewrite and buffers) and change the two together, in the file. Without a `maxmemory`, Redis grows until the container is killed instead of refusing a write.
A full queue Redis refuses writes on purpose, and **`RedisQueueMemoryHigh`** fires at 80% (for 10 minutes) so it is seen first. It reads the exporter (`redis-exporter`) the otel agent scrapes; it does not watch the cache, which is meant to sit near its limit.
Measured on a 2 MB instance with these flags: at the limit the 472nd 2 KB stream entry was refused, 471 were kept, none evicted, and reads still worked.

Both services use the **Redis Stack** image (`redis/redis-stack-server`, pinned by digest), not a plain `redis`. The queue Redis does not need the modules, but the AOF written by a server of this image holds the semantic cache's search and JSON commands, and a plain Redis exits with an error trying to load it (tried). Move both to `redis:8`, which has the query engine built in,
after `tests/integration/test_queue_real_redis.py` and the semantic-cache integration tests pass against it. Server flags reach the Stack image through `REDIS_ARGS`: it has no entrypoint, so a compose `command:` would replace its start script.

**Migrating an existing deployment: do this BEFORE you pull the new compose file.** Turning AOF on for a volume that holds only a snapshot starts the server **empty**: Redis ignores `dump.rdb` once AOF is on (measured: the key was gone). The queue would start empty and, worse, the front door's **revocation list would be lost**, so a person you revoked could use their token again until it expired. The way to keep the data is to turn AOF on in the running server first, which writes the dataset out as an AOF the new service then loads:

```
dc exec redis redis-cli config set appendonly yes
dc exec redis redis-cli info persistence | grep -E 'aof_rewrite_in_progress|aof_last_bgrewrite_status'   # wait for 0 and ok, then continue
git pull && dc pull && dc up -d      # the new redis service starts from that AOF; redis-cache starts empty; api and workers are recreated to read SEMANTIC_CACHE_REDIS_URL
dc exec redis redis-cli config get appendonly         # yes
dc exec authlimit authlimit revoke --list             # the revocation list is still there
```

Tried on real containers with the shipped image: after those steps a stream entry, a lock, a revocation key, a JSON document and a search index were all still there. The old semantic cache's keys move across inside the queue Redis's data and expire within `SEMANTIC_CACHE_TTL_SECONDS` (an hour); the new cache starts cold, which costs model calls for an hour and nothing else.
**Not run on a droplet.** The compose file was resolved (`docker compose config`) and each service's flags were applied and read back on real containers, but this exact sequence on the live stack is the first time it meets your volume: do it in a quiet hour.

**Bumping the Redis, Qdrant and exporter images.** All four are pinned by digest in the compose files (the tag is only for the reader). Pull the one you want, read its digest (`docker image inspect <image> --format '{{index .RepoDigests 0}}'`), and put it in the production **and** the development compose file (the tests start the development file's Redis and Qdrant: `tests/containers.py::_shipped_image`). Run `make test-integration` before a production bump.

**Not done:** a password (`requirepass`) for either Redis. Neither publishes a port in production, so it is defence in depth, and it is optional in the work order; it would mean `REDIS_URL=redis://:<password>@redis:6379` in every service that reads it, `AUTHLIMIT_REDIS_URL` and the exporter included.

## After a database restart

Both database pools (`appdata` and `checkpointer`) are `SelfHealingPool`s (PRODUCTION_REVIEW.md R25). When PgBouncer or Postgres restarts, every pooled connection is dead; the plain pool finds out one at a time and fails one request per dead connection (up to the pool's size). Now the first connection that turns out to be broken makes the pool test and replace all its idle connections in one pass: **expect about two failed requests after a restart, not a pool's worth**, and none that hang.
`agent_db_pool_connection_lost_total{pool}` counts them: a burst is a restart, a steady rate is a database that keeps dropping connections. There is no alert on it. (`psycopg_pool`'s own `check=` was tried and rejected: with every connection dead the first request waits out the 30 s pool timeout; ENGINEERING_PATTERNS.md 102.)

## Learning from production answers

Two advisory signals, both from real traffic (PRODUCTION_REVIEW.md R28; the offline tier, `make eval` and promptfoo, stays the gate).

**Ratings.** `POST /chat/feedback` stores a thumbs up or down and an optional comment on one answer, for the signed-in person's own conversation (anyone else's is a 404). It is counted as `agent_feedback_total{rating}` (a person changing their mind replaces their rating and is not counted twice) and kept in `chat_feedback` for a reviewer to read; nothing the agent does depends on it,
and the comment is never logged. **An existing deployment needs the table once**: `/health/ready` warns that it is missing and names the script. Apply it:

```
dc exec -T postgres psql -U langfuse -d postgres -f /docker-entrypoint-initdb.d/26-chat-feedback.sql      # idempotent
```

The web page and the CLI have no thumbs yet, and a rating is not yet attached to a trace as a score: that waits for traces in production (R17 part 2). Read the ratings with SQL, for example `SELECT rating, count(*) FROM chat_feedback WHERE created_at > now() - interval '7 days' GROUP BY 1`.

**The answer sample.** `make answer-sample` (`ARGS="--date YYYY-MM-DD"`; yesterday UTC by default) reads up to `ANSWER_SAMPLE_SIZE` (50) random conversations that were last active that day and runs the grounded-claims checks the graph runs on every turn (`count_ungrounded_claims`, `find_likely_uncited`, `find_likely_misattributed`) over each one's final answer and citations, from the checkpoint. It calls no model.
It exports **counts**, `agent_answer_sample_total{outcome=judged|no_citations|unreadable}` and `agent_answer_sample_finding_total{finding=ungrounded_marker|uncited|misattributed}`, so a rate is `finding / judged`. Nothing alerts on them: the checks are heuristics tuned on small models, a trend to watch and not a verdict. Cron, once you have decided:

```
30 4 * * * cd /opt/graph-agent-core && deploy/prod/compose.sh run --rm --no-deps -T api python -m scripts.jobs.answer_sample
```

**It reads answers across tenants.** Like the retention sweep it is an operator job, never reachable from a request, but unlike it, it reads CONTENT: the final answer and its citations of each sampled conversation. It emits and logs no text, tenant, person or thread id (tested, including the log records' structured fields). If your data policy does not allow an operator job to read answers, leave it unscheduled, or set `ANSWER_SAMPLE_TENANTS` to the tenants that agree.
A conversation counts as the day's if its last message was that day, and only its LAST turn is judged.

## Trimming conversations

**`make checkpoint-sweep`** deletes a conversation (every checkpoint of it, and its row in the session directory) `CHECKPOINT_RETENTION_DAYS` after its last message. **The default is 0, which is off**:
nothing is deleted until you choose a number, and until you do, the checkpointer's tables grow with every step of every turn (each change to the message list stores the whole list again). Run it by hand
with `ARGS=--dry-run` first: it prints how many conversations it would delete and changes nothing. A real run needs the checkpointer database (it opens the saver itself); it prints what it did, and says when it
stopped at its ceiling of 50,000 conversations (run it again). Nothing schedules it; from cron once the retention policy is decided:

```
30 3 * * * cd /opt/graph-agent-core && deploy/prod/compose.sh run --rm --no-deps -T api python -m scripts.jobs.checkpoint_sweep
```

(`-T` because cron has no terminal, and `api` because it is the service the other one-off commands in this file run from. That line was written from the compose file and the other commands here, not run on a droplet: try it by hand with `--dry-run` before relying on it.)

A person can also delete one of their own conversations with `DELETE /chat/sessions/{id}` (it refuses while a turn is running on it). That route acts only for the signed-in person: there is no operator route that deletes one named person's conversations on request, so a deletion request has this sweep (everything past a number of days) or the person's own delete to rely on.

It does not touch the usage events (`make usage-events-sweep`), the wallet, a person's cross-session memories, the gateway's spend logs (`store_prompts_in_spend_logs` is off in production) or Langfuse, and it
does not trim old checkpoints inside a conversation that is still in use. A conversation that gets a message while the sweep runs keeps its row, but may lose its older history. One that was paused at an
approval is deleted with the rest, and the approval can no longer be given, which runs nothing.

## Credit billing (optional)

Everything about credit billing (turning it on, changing a wallet, reconciling the wallet, selling a credit pack with Stripe or Polar, its dashboard and alerts) is the billing package's:
[`packages/billing/docs/RUNBOOK.md`](../packages/billing/docs/RUNBOOK.md). Deployment specifics that are this repository's: apply the package's schema to an existing volume with `make billing-migrate`
(`make billing-migrate-status ARGS=--check` is the deploy gate: `prod/release.sh` runs it when `.env` sets `CREDITS_PER_USD`), before the code that uses it, and start its workers from the app image as shown above. `deploy/prod/app.yml` does not yet pass `CREDITS_PER_USD` or the other billing variables to any container, so turning billing on in production needs that wiring first.

## The crawler's network

The crawler (`crawl4ai`, headless Chromium; `app/adapters/outbound/crawl4ai/web_crawler.py`) is **opt-in in production, and only behind a host firewall.** The app checks the first URL it hands over
(`app/core/url_safety.py`); the browser then follows redirects and loads sub-resources by itself, so a page that passes the check can send it to `http://qdrant:6333`, `http://litellm:4000` or the
droplet's metadata address (`169.254.169.254`: instance credentials). Isolation for that is the network's job, not the application's (PRODUCTION_REVIEW.md R20, specs/009 A1).

**Two layers, one in each place.**

1. **A network of its own** (`deploy/prod/tools.yml`, `deploy/dev/tools.yml`): `crawl4ai` is alone on `crawler`, and the three app roles that call it (`api`, `agent-worker`, `ingest-worker`) are on
   `crawler` and on `default`. A container on `crawler` alone cannot resolve or reach one on `default`, by name or by address (checked on a real Docker daemon, with stand-in containers). Production
   pins the subnet (`172.30.50.0/24`), publishes no port, and requires `CRAWL4AI_API_TOKEN` (crawl4ai 0.9+ refuses to listen beyond loopback without one).
2. **A host firewall for what a bridge network still reaches.** A bridge network has internet egress, and on a cloud droplet that includes the metadata service and the VPC. Docker offers no option
   for this: `deploy/prod/crawler-firewall.sh` writes rules in the host's `DOCKER-USER` chain (traffic the crawler sends through the host: link-local, `10/8`, `172.16/12`, `192.168/16` and
   `100.64/10` are dropped; its own subnet and the internet are allowed) and in `INPUT` (traffic to the host itself, which `DOCKER-USER` never sees: only replies are accepted).

**Turning it on** (on the app droplet, as the deploy user):

```
deploy/prod/crawler-firewall.sh install      # a systemd unit: the rules are applied after Docker starts, and again whenever Docker restarts
echo 'CRAWLER_ENABLED=true' >> .env          # and CRAWL4AI_API_TOKEN=<a long random value>; the exact line, with no spaces or comment after it
dc up -d                                     # compose.sh adds deploy/prod/tools.yml, and refuses to start anything while the firewall is missing
deploy/prod/crawler-firewall.sh check        # probes one address in each blocked range from inside the crawler and reads the rules' packet counters
```

`check` is the proof, and it reads **counters**: a timeout alone cannot tell "dropped" from "nothing there", so a probe passes only if the rule for its range counted a packet; it also confirms the
internet still works. `status` is `check` without the probing (exit 0 only if every rule is in place), and `remove` deletes the rules. A refused start prints what to run.

**What is and is not verified.** The script is tested against a stateful fake `iptables` (`tests/scripts/deploy/test_crawler_firewall.py`: it is idempotent, `status` names the missing rule, `check` fails
unless every range counted a drop), and `compose.sh` against a fake `docker` (`test_compose_wrapper.py`). On a real Docker daemon, a container on the pinned subnet reached a service on the host before
`apply` and could not after, with each range's counter moving. **It has not been run on a DigitalOcean droplet**: the metadata address, the VPC and the droplet's resolvers are what a laptop cannot show,
so run `check` there before relying on it, and look at `iptables -S DOCKER-USER` for rules of your own that sit above it. No browser-level crawl of a hostile page was run either: the probes are `curl`
from inside the crawler's container.

**What it does not do.** The development stack is weaker: it publishes its infrastructure on the host's ports (Qdrant, Redis, Postgres and others), and a bridge network reaches the host through its
gateway (`host.docker.internal` on Docker Desktop), so on a developer's machine the crawler can still reach those. Do not point the dev crawler at a page you do not trust; the dev droplet does not
install the firewall either, and nothing public faces that box. The application's own check on the first URL stays: it is the first line, not the only one.

## The dev droplet

`deploy/terraform/dev/` is the whole dev stack on **one** DigitalOcean droplet (default `s-8vcpu-16gb`; the ML service alone takes about 3 cores and 2 GB, and the crawler image about 9 GB of disk), for
the work a laptop is too small for or that must run while the laptop is off. It is the dev compose project, unchanged, with one overlay: the LLM is a **hosted endpoint** (any OpenAI-compatible
one), as in production, because a droplet has no GPU and no native Ollama.

- **Nothing public.** The cloud firewall opens SSH from `admin_ip_cidrs` and nothing else (the variable refuses `0.0.0.0/0`), because the dev stack publishes its services on every interface
  with passwords like `admin/admin` and `sk-anything`. You reach it through an SSH tunnel: `make droplet-tunnel` forwards the front door, Grafana, Langfuse, LiteLLM and Open WebUI, so
  `https://api.localhost:8443/` works as on a laptop. There is no reserved IP, VPC or domain, and no CI deploys to it.
- **The files**: `deploy/dev/hosted-llm.yml` (the overlay: LiteLLM runs `deploy/prod/litellm-config.yaml`, so the aliases are production's) and `deploy/dev/droplet.env.example` (the `LLM_*` variables,
  with the names and order of `deploy/prod/prod.env.example`). The droplet's `.env` is separate from the laptop's `.env.example` on purpose: `LLM_API_KEY` and `LLM_API_BASE` mean something else for
  `make strix` there, and a laptop's keys must not travel.

```
cd deploy/terraform/dev && cp terraform.tfvars.example terraform.tfvars   # do_token, ssh_key_fingerprints, deploy_ssh_public_key, admin_ip_cidrs
terraform init && terraform apply                                          # one droplet, one firewall, one project
cd ../../.. && export DEV_DROPLET=$(terraform -chdir=deploy/terraform/dev output -raw droplet)   # deploy@<ip>
make droplet-sync                      # copies exactly the files git knows about (never a .env) to /opt/graph-agent-core-dev
make droplet-ssh                       # then, on the droplet: cp deploy/dev/droplet.env.example .env, and fill in LLM_API_KEY; log out
make droplet-up                        # build and start the whole stack there (make up-all with the overlay)
make droplet-bootstrap droplet-ingest  # the Casdoor tenants and demo users; the sample documents and the skills catalog
make droplet-ca droplet-tunnel         # fetch the droplet's Caddy root certificate (trust it on your machine), then open the tunnel
```

`make droplet-down` stops the containers and keeps the volumes. `terraform destroy` in `deploy/terraform/dev` removes the droplet, and it is **billed hourly until you do**. The droplet gets production's
cloud-init hardening (a deploy user, ufw, fail2ban, Docker) from the same template, `deploy/terraform/cloud-init.tpl.yaml`, which takes the ports to open and extra packages as parameters; production's
rendering is byte-identical to before (checked by rendering `user_data` and comparing hashes, since a changed one makes DigitalOcean replace the droplet).

**What is and is not verified.** `terraform fmt`, `validate` and Checkov pass for both roots, the template renders, and the hosted-LLM overlay was started on a real Docker daemon and LiteLLM served through it.
**`terraform apply` and a real droplet build have not been run**, so the first run is the real test: the size, the disk, and the tunnel are what to watch.

## Security scanning in front of all this

- **Checkov** (`.checkov.yaml`, `make checkov`) scans
  `deploy/terraform/**` for IaC misconfiguration before you ever `apply`.
- **Semgrep** (`make semgrep`) and **Trivy** (`make trivy`) scan the source,
  Dockerfiles and dependencies of what this workflow ships. CI also scans each
  built image, and once a week the images production runs
  ([`security.yml`](../.github/workflows/security.yml)).
- **SonarQube** (`make sonar-up`/`sonar-scan`, local only) is a code-quality
  gate on the same source — see the root README's "Security scanning and load
  testing" section.

None of these scan the LIVE droplet itself (no DAST here) — they gate what
gets built and shipped to it, not the running instance.
