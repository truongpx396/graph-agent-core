"""Put a backup directory in the object-storage bucket, and say how old the newest one is (PRODUCTION_REVIEW.md R26).

    python -m scripts.tools.backup_upload /backup --prefix backups/20261115T021700Z      # upload every file, then a manifest
    python -m scripts.tools.backup_upload --latest --max-age-hours 30                    # exit 1 if the newest backup is older, or there is none
    python -m scripts.tools.backup_upload --download backups/20261115T021700Z --to /restore   # fetch a backup back, every file checked against its manifest

`deploy/prod/backup.sh` runs the first from the app image (it has the object-storage settings). A backup is COMPLETE if and only if its `manifest.json` exists:
it is uploaded last, after every file, and lists each file's size and SHA-256, so a run that died halfway leaves files with no manifest and `--latest` does not count
it. The bucket is the uploads' own (`MINIO_BUCKET`), under `backups/`: set a lifecycle rule on that prefix in the Spaces console to expire old ones. Nothing here deletes.

`--latest` is the check to run from somewhere that is not the droplet (another cron, an uptime monitor): a backup job that stopped running is the failure nothing else reports.
`--download` is the first step of a restore on a rebuilt droplet: it refuses a backup with no manifest, and fails on a file whose size or SHA-256 is not the manifest's.

EXIT CODES. 0 done / the newest backup is recent enough | 1 an error, or no recent backup | 2 usage.
"""
import argparse
import hashlib
import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.adapters.outbound.minio import object_store
from app.bootstrap import wire
from app.core.config import MINIO_BUCKET

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
BACKUP_PREFIX = "backups/"
MANIFEST = "manifest.json"
_CHUNK = 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def upload(directory: Path, prefix: str, *, client: Any = None) -> dict[str, Any]:
    """Uploads every file in `directory` under `prefix/`, then the manifest, and returns the manifest. Raises on the first failure, before the manifest: an incomplete
    upload must not look complete."""
    if not prefix.startswith(BACKUP_PREFIX) or prefix.endswith("/"):
        raise ValueError(f"prefix must start with {BACKUP_PREFIX!r} and not end in '/': {prefix!r}")
    files = sorted(p for p in directory.iterdir() if p.is_file())
    if not files:
        raise FileNotFoundError(f"nothing to upload in {directory}")
    client = client or object_store.get_client()
    object_store.ensure_bucket(client)
    entries = {}
    for path in files:
        client.fput_object(MINIO_BUCKET, f"{prefix}/{path.name}", str(path))
        entries[path.name] = {"bytes": path.stat().st_size, "sha256": _sha256(path)}
    manifest = {"created_at": datetime.now(UTC).isoformat(), "files": entries}
    body = json.dumps(manifest, indent=2).encode()
    client.put_object(MINIO_BUCKET, f"{prefix}/{MANIFEST}", io.BytesIO(body), length=len(body), content_type="application/json")
    return manifest


def latest(*, client: Any = None) -> tuple[str, datetime] | None:
    """`(prefix, when)` of the newest COMPLETE backup (the one with a manifest), or None."""
    client = client or object_store.get_client()
    object_store.ensure_bucket(client)
    newest: tuple[str, datetime] | None = None
    for obj in client.list_objects(MINIO_BUCKET, prefix=BACKUP_PREFIX, recursive=True):
        name = obj.object_name or ""
        if not name.endswith(f"/{MANIFEST}") or obj.last_modified is None:
            continue
        if newest is None or obj.last_modified > newest[1]:
            newest = (name[: -len(f"/{MANIFEST}")], obj.last_modified)
    return newest


def download(prefix: str, directory: Path, *, client: Any = None) -> dict[str, Any]:
    """Fetches the backup under `prefix` into `directory` and returns its manifest. Raises `FileNotFoundError` for a backup with no manifest (an upload that never finished)
    and `ValueError` for a file whose size or SHA-256 is not the manifest's; a file that failed the check is removed, so the directory never holds one that looks usable."""
    if not prefix.startswith(BACKUP_PREFIX) or prefix.endswith("/"):
        raise ValueError(f"prefix must start with {BACKUP_PREFIX!r} and not end in '/': {prefix!r}")
    client = client or object_store.get_client()
    try:
        response = client.get_object(MINIO_BUCKET, f"{prefix}/{MANIFEST}")
        try:
            manifest: dict[str, Any] = json.loads(response.read())
        finally:
            response.close()
            response.release_conn()
    except Exception as exc:
        raise FileNotFoundError(f"no complete backup at {prefix}/ (no readable {MANIFEST}): {type(exc).__name__}") from exc
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in manifest["files"].items():
        target = directory / name
        client.fget_object(MINIO_BUCKET, f"{prefix}/{name}", str(target))
        if target.stat().st_size != expected["bytes"] or _sha256(target) != expected["sha256"]:
            target.unlink()
            raise ValueError(f"{name} does not match the manifest (size or SHA-256): the copy in the bucket is damaged or was changed")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("directory", nargs="?", help="the backup directory to upload")
    parser.add_argument("--prefix", help="where in the bucket, e.g. backups/20261115T021700Z (required with a directory)")
    parser.add_argument("--latest", action="store_true", help="report the newest complete backup instead of uploading")
    parser.add_argument("--download", metavar="PREFIX", help="fetch the backup under PREFIX (e.g. backups/20261115T021700Z) into --to, checking every file against its manifest")
    parser.add_argument("--to", help="with --download: the directory to put the backup in")
    parser.add_argument("--max-age-hours", type=float, default=30.0, help="with --latest: fail if the newest is older than this (default 30)")
    args = parser.parse_args(argv)
    modes = [bool(args.directory), args.latest, bool(args.download)]
    if modes.count(True) != 1:
        parser.error("give exactly one of: a directory to upload, --latest, --download PREFIX")
    if args.directory and not args.prefix:
        parser.error("--prefix is required with a directory")
    if args.download and not args.to:
        parser.error("--to is required with --download")
    wire()
    if args.download:
        try:
            manifest = download(args.download, Path(args.to))
        except (OSError, ValueError) as exc:
            print(f"backup download failed: {exc}", file=sys.stderr)
            return EXIT_ERROR
        total = sum(f["bytes"] for f in manifest["files"].values())
        print(f"downloaded {len(manifest['files'])} files, {total} bytes, to {args.to} (every file matches its manifest)")
        return EXIT_OK
    if args.latest:
        found = latest()
        if found is None:
            print("no complete backup in the bucket", file=sys.stderr)
            return EXIT_ERROR
        prefix, when = found
        age = datetime.now(UTC) - when
        print(f"{prefix} ({age.total_seconds() / 3600:.1f} hours old)")
        return EXIT_OK if age <= timedelta(hours=args.max_age_hours) else EXIT_ERROR
    try:
        manifest = upload(Path(args.directory), args.prefix)
    except (OSError, ValueError) as exc:
        print(f"backup upload failed: {exc}", file=sys.stderr)
        return EXIT_ERROR
    total = sum(f["bytes"] for f in manifest["files"].values())
    print(f"uploaded {len(manifest['files'])} files, {total} bytes, to {args.prefix}/")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
