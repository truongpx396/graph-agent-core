"""Snapshot every Qdrant collection to files, and restore them (PRODUCTION_REVIEW.md R26).

    python -m scripts.tools.qdrant_backup --out /backup          # a snapshot of each collection, and the aliases, into /backup
    python -m scripts.tools.qdrant_backup --restore /backup      # recreate the collections and the aliases from /backup

Qdrant's image has no `curl`, so the snapshot is taken from the app image over the compose network (`deploy/backup/backup.sh`). A snapshot is made per PHYSICAL
collection (`docs-<hash>`) and does not carry the alias the app reads and writes through (`docs-live`, pattern 67), so the aliases are written beside the snapshots
and put back on restore: a restored collection the app cannot reach by its alias would look empty. The snapshot stays on the server only for the length of the
download (it is deleted after), so repeated backups do not fill Qdrant's disk.

A restore refuses a collection that already exists unless `--overwrite`: it replaces every point in it. It never deletes a collection it did not restore.

EXIT CODES. 0 done | 1 an error, or a collection that exists (named) | 2 usage.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import quote

import httpx
from qdrant_client.models import (
    CreateAlias,
    CreateAliasOperation,
    DeleteAlias,
    DeleteAliasOperation,
)

from app.adapters.outbound.qdrant import qdrant_store
from app.bootstrap import wire
from app.core.config import QDRANT_URL

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
ALIASES_FILE = "qdrant-aliases.json"
_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=None, pool=None)  # a snapshot of a large collection takes as long as it takes
_CHUNK = 1024 * 1024


def _snapshot_file(collection: str) -> str:
    return f"qdrant-{collection}.snapshot"


async def backup(out: Path) -> list[str]:
    """The lines to print. One snapshot file per collection, plus the aliases. Raises on any failure: a backup that is missing a collection is not a backup."""
    out.mkdir(parents=True, exist_ok=True)
    client = qdrant_store.get_client()
    collections = sorted(c.name for c in (await client.get_collections()).collections)
    lines = []
    async with httpx.AsyncClient(base_url=QDRANT_URL, timeout=_TIMEOUT) as http:
        for name in collections:
            snapshot = await client.create_snapshot(collection_name=name)
            if snapshot is None:
                raise RuntimeError(f"Qdrant made no snapshot of {name!r}")
            target = out / _snapshot_file(name)
            try:
                async with http.stream("GET", f"/collections/{quote(name, safe='')}/snapshots/{quote(snapshot.name, safe='')}") as response:
                    response.raise_for_status()
                    with target.open("wb") as handle:
                        async for chunk in response.aiter_bytes(_CHUNK):
                            handle.write(chunk)
            finally:
                await client.delete_snapshot(collection_name=name, snapshot_name=snapshot.name)  # whether or not the download worked: do not fill the server's disk
            lines.append(f"{name}: {target.stat().st_size} bytes")
    aliases = [{"alias_name": a.alias_name, "collection_name": a.collection_name} for a in (await client.get_aliases()).aliases]
    (out / ALIASES_FILE).write_text(json.dumps(aliases, indent=2), encoding="utf-8")
    lines.append(f"aliases: {len(aliases)}")
    return lines


async def restore(source: Path, *, overwrite: bool = False) -> list[str]:
    """Recreates each collection from its snapshot file, then the aliases. Raises `FileExistsError` for a collection that exists (before it touches any), unless `overwrite`."""
    files = sorted(source.glob("qdrant-*.snapshot"))
    if not files:
        raise FileNotFoundError(f"no qdrant-*.snapshot files in {source}")
    names = [f.name[len("qdrant-") : -len(".snapshot")] for f in files]
    client = qdrant_store.get_client()
    existing = {c.name for c in (await client.get_collections()).collections}
    clashes = sorted(set(names) & existing)
    if clashes and not overwrite:
        raise FileExistsError(f"already exists: {', '.join(clashes)} (restoring replaces every point in it; pass --overwrite to do that)")
    lines = []
    async with httpx.AsyncClient(base_url=QDRANT_URL, timeout=_TIMEOUT) as http:
        for name, path in zip(names, files, strict=True):
            with path.open("rb") as handle:
                response = await http.post(
                    f"/collections/{quote(name, safe='')}/snapshots/upload",
                    params={"priority": "snapshot"},
                    files={"snapshot": (path.name, handle, "application/octet-stream")},
                )
            response.raise_for_status()
            count = (await client.count(collection_name=name, exact=True)).count
            lines.append(f"{name}: restored, {count} points")
    aliases_path = source / ALIASES_FILE
    aliases = json.loads(aliases_path.read_text(encoding="utf-8")) if aliases_path.exists() else []
    for entry in aliases:
        if entry["collection_name"] in names:
            await _set_alias(entry["alias_name"], entry["collection_name"])
            lines.append(f"alias {entry['alias_name']} -> {entry['collection_name']}")
    qdrant_store.reset_identity_cache()  # the app caches which physical collection an alias points at
    return lines


async def _set_alias(alias: str, collection: str) -> None:
    """Makes `alias` point at `collection` in one atomic request (replacing where it exists), by the name as recorded: not through `repoint_live`, which builds a `-live` name."""
    client = qdrant_store.get_client()
    operations: list[DeleteAliasOperation | CreateAliasOperation] = []
    if any(a.alias_name == alias for a in (await client.get_aliases()).aliases):
        operations.append(DeleteAliasOperation(delete_alias=DeleteAlias(alias_name=alias)))
    operations.append(CreateAliasOperation(create_alias=CreateAlias(collection_name=collection, alias_name=alias)))
    await client.update_collection_aliases(change_aliases_operations=operations)


async def _run(args: argparse.Namespace) -> int:
    try:
        lines = await backup(Path(args.out)) if args.out else await restore(Path(args.restore), overwrite=args.overwrite)
    except (FileExistsError, FileNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    finally:
        await qdrant_store.close_client()
    print("\n".join(lines))
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", help="a directory to write a snapshot of each collection (and the aliases) into")
    mode.add_argument("--restore", help="a directory holding the snapshots (and aliases) to restore")
    parser.add_argument("--overwrite", action="store_true", help="with --restore: replace a collection that already exists")
    args = parser.parse_args(argv)
    if args.overwrite and not args.restore:
        parser.error("--overwrite is only for --restore")
    wire()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
