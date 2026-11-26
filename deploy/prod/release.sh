#!/usr/bin/env bash
# Put one published release on the production app droplet: deploy/prod/release.sh <commit-sha>
#
# .github/workflows/deploy.yml runs it after syncing the config of that same commit, and an operator can run it by hand to roll back to an older
# one. It records what it started in .image-tag, which compose.sh reads, so a later `dc up -d` keeps running that release instead of `:latest`.
set -euo pipefail

cd "$(dirname "$0")/../.."   # the repo root, as compose.sh

tag=${1:-}
if [[ ! $tag =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: deploy/prod/release.sh <full commit sha>" >&2
  exit 2
fi
export IMAGE_TAG=$tag
previous=$(cat .image-tag 2>/dev/null || true)

deploy/prod/compose.sh pull

# The billing package's schema is applied by hand (packages/billing/docs/RUNBOOK.md). Once credits have a price, code newer than the schema must not
# start: the check exits 1 and the running release stays as it is.
if grep -qE '^CREDITS_PER_USD=.+' .env 2>/dev/null; then
  deploy/prod/compose.sh run --rm --no-deps api python -m credit_billing.cli.migrate --check || {
    echo "billing schema is not current: run it first (python -m credit_billing.cli.migrate), then release again" >&2
    exit 1
  }
fi

deploy/prod/compose.sh up -d --remove-orphans
printf '%s\n' "$tag" > .image-tag

# Through the front door, as a client reaches it: the API publishes no host port. API_DOMAIN is in the droplet's own .env, which CI never sees.
# 120 s: a first release also waits for Let's Encrypt to issue the certificates.
api=$(grep '^API_DOMAIN=' .env | cut -d= -f2-) || { echo "API_DOMAIN is not set in .env: the release is up but cannot be checked" >&2; exit 1; }
# shellcheck disable=SC2016  # $API is for the inner shell to expand: it is passed in the environment, not spliced into the command
if ! API="$api" timeout 120 bash -c 'until curl -sf "https://$API/health/ready" >/dev/null; do sleep 3; done'; then
  echo "release $tag did not become ready" >&2
  if [[ -n $previous && $previous != "$tag" ]]; then
    echo "to roll back: run the Deploy workflow with image_tag=$previous (images and config together), or here: deploy/prod/release.sh $previous" >&2
  fi
  exit 1
fi

# Only after the new release is ready: images unused for a week go, so the disk does not fill with one set per deploy.
docker image prune -af --filter until=168h
