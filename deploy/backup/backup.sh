#!/usr/bin/env bash
# The production backup (PRODUCTION_REVIEW.md R26): the four Postgres databases, a Qdrant snapshot per collection and the queue Redis, taken on the app droplet,
# checked, copied off the box to the object-storage bucket, and pruned locally. deploy/README.md ("Backups and restore") is the runbook; the restore it describes was
# performed (`make restore-drill`).
#
#   17 2 * * *  /opt/graph-agent-core/deploy/backup/backup.sh >> /var/log/graph-agent-backup.log 2>&1
#
# Run as the deploy user, from cron. It needs: the production stack up, the object-storage settings in .env (the same Spaces credentials the uploads use), and
# BACKUP_DIR (default /var/backups/graph-agent-core) on a disk with room for a few copies. Exit status is non-zero on ANY failure, so cron's mail or a wrapper can say so.
#
# What it takes, and how:
#   appdata, checkpointer, litellm   pg_dump --format=custom, inside the `postgres` container (the app image has no Postgres client)
#   casdoor                          the same, inside `casdoor-db` (who can sign in, and their tokens' keys)
#   qdrant                           a snapshot of every collection, downloaded and deleted server-side (scripts/tools/qdrant_backup.py, from the app image: Qdrant's has no curl)
#   redis                            `redis-cli --rdb -`: the queue, the locks and the front door's revocation list. The answer cache is NOT backed up (it is disposable)
# Each dump is read back with `pg_restore --list` before the backup counts, and the upload is complete only when its manifest exists (scripts/tools/backup_upload.py).
#
# BACKUP_SKIP_UPLOAD=1 keeps the backup on this box only: a backup on the same disk as the data is not one, so it is for a first try and nothing else.
set -euo pipefail

cd "$(dirname "$0")/../.."   # the repo root: compose resolves its paths and .env from here
dc() { docker compose --project-directory . -f deploy/compose/docker-compose.prod.yml "$@"; }

ROOT="${BACKUP_DIR:-/var/backups/graph-agent-core}"
KEEP="${BACKUP_KEEP_LOCAL:-3}"
case "$ROOT" in /?*) ;; *) echo "BACKUP_DIR must be an absolute path, got '$ROOT'" >&2; exit 2;; esac
case "$KEEP" in ''|*[!0-9]*) echo "BACKUP_KEEP_LOCAL must be a number, got '$KEEP'" >&2; exit 2;; esac
[ "$KEEP" -ge 1 ] || { echo "BACKUP_KEEP_LOCAL must be at least 1" >&2; exit 2; }

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PARTIAL="$ROOT/$STAMP.partial"   # renamed to $ROOT/$STAMP only when everything below worked: a directory without the suffix is a complete backup
START="$(date +%s)"
mkdir -p "$PARTIAL"
echo "[$STAMP] backup started -> $PARTIAL"

for db in appdata checkpointer litellm; do
  dc exec -T postgres pg_dump -U langfuse --format=custom "$db" > "$PARTIAL/$db.dump"
  dc exec -T postgres pg_restore --list < "$PARTIAL/$db.dump" > /dev/null   # fails on a truncated or corrupt dump
done
dc exec -T casdoor-db pg_dump -U casdoor --format=custom casdoor > "$PARTIAL/casdoor.dump"
dc exec -T casdoor-db pg_restore --list < "$PARTIAL/casdoor.dump" > /dev/null

dc exec -T redis redis-cli --rdb - > "$PARTIAL/redis.rdb"
[ "$(head -c 5 "$PARTIAL/redis.rdb")" = "REDIS" ] || { echo "the Redis dump does not start with REDIS: not an RDB" >&2; exit 1; }

# As this user, so the files are ours and not a container user's; the snapshots are written straight into the backup directory.
dc run --rm --no-deps -T --user "$(id -u):$(id -g)" -v "$PARTIAL:/backup" api python -m scripts.tools.qdrant_backup --out /backup

if [ "${BACKUP_SKIP_UPLOAD:-0}" != "1" ]; then
  dc run --rm --no-deps -T --user "$(id -u):$(id -g)" -v "$PARTIAL:/backup:ro" api python -m scripts.tools.backup_upload /backup --prefix "backups/$STAMP"
else
  echo "BACKUP_SKIP_UPLOAD=1: this backup stays on this disk only"
fi

mv "$PARTIAL" "$ROOT/$STAMP"
echo "[$STAMP] backup complete in $(( $(date +%s) - START )) s: $(du -sh "$ROOT/$STAMP" | cut -f1)"

# Prune: keep the newest $KEEP complete backups, and any .partial older than two days (a run that died). Only names this script makes are matched.
# Newest first, skip the first $KEEP: `tail -n +N` is portable where `head -n -N` is GNU-only.
# shellcheck disable=SC2012
ls -1d "$ROOT"/[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]T[0-9][0-9][0-9][0-9][0-9][0-9]Z 2>/dev/null | sort -r | tail -n "+$((KEEP + 1))" | while read -r old; do
  echo "pruning $old"
  rm -rf -- "$old"
done
find "$ROOT" -maxdepth 1 -name '*.partial' -mtime +2 -print -exec rm -rf -- {} +
