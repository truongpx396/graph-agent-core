#!/usr/bin/env bash
# `docker compose` on the production project, from anywhere: deploy/prod/compose.sh pull | up -d | ps | logs api ...
#
# By default it runs exactly `docker compose --project-directory . -f deploy/prod/docker-compose.yml <args>`. When the droplet's .env says CRAWLER_ENABLED=true it adds the crawler
# overlay (deploy/prod/tools.yml) to EVERY command, so a deploy or an operator's `up` cannot drop the crawler (`up --remove-orphans` would remove it) or recreate the app roles
# without its network. And it refuses to START anything while the crawler's firewall is missing: a crawler without it can be steered to the droplet's metadata address.
# .github/workflows/deploy.yml and the `dc` alias in deploy/README.md both go through this.
set -euo pipefail

cd "$(dirname "$0")/../.."   # the repo root: compose resolves its paths and .env from here
files=(-f deploy/prod/docker-compose.yml)

if grep -qx 'CRAWLER_ENABLED=true' .env 2>/dev/null; then
  case "${1:-}" in
    up | start | restart | create | run)
      deploy/prod/crawler-firewall.sh status >/dev/null || {
        echo "CRAWLER_ENABLED=true in .env, but the crawler's firewall is not installed: run deploy/prod/crawler-firewall.sh install first" >&2
        exit 1
      }
      ;;
  esac
  files+=(-f deploy/prod/tools.yml)
fi

exec docker compose --project-directory . "${files[@]}" "$@"
