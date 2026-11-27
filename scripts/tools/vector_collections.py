"""Which embedding model each vector collection holds, for a person. Read-only unless `--record`.

    make vector-collections                    # or: python -m scripts.tools.vector_collections
    make vector-collections ARGS=--record      # also writes the identity onto a collection that predates the metadata

A collection is named `<logical>-<8 hex>` after the embedding identity its vectors were made with (pattern 67), and `<logical>-live` points at
the one in use. The 8 digits say whether two match, not what either is; what is, the collection keeps as metadata. This prints both, beside
the identity this process is configured for, including the collections a re-embed left behind for you to delete.

`--record` writes only where the name's hash EQUALS the configured identity's: that proves what the collection holds. One built under other
settings stays unrecorded rather than guessed. It reads no points, so no document text and nothing tenant-specific is printed (Principle V).
"""
import argparse
import asyncio
import sys
from typing import Any

from app.adapters.outbound.embeddings import embedding_identity
from app.adapters.outbound.qdrant import qdrant_store
from app.bootstrap import wire
from app.core.config import COLLECTION, SKILLS_COLLECTION


def _describe(identity: dict[str, Any]) -> str:
    text = f"{identity.get('model')}, {identity.get('dim')} dimensions"
    for label, key in (("query prefix", "query_prefix"), ("document prefix", "document_prefix"), ("revision", "revision")):
        if identity.get(key):
            text += f", {label} {identity[key]!r}"
    return text


async def report(*, record: bool = False) -> list[str]:
    configured = await embedding_identity.current_identity()
    expected = configured.hash8()
    client = qdrant_store.get_client()
    names = sorted(c.name for c in (await client.get_collections()).collections)
    lines = [f"configured identity {expected}: {_describe(configured.describe())}"]
    for logical in (COLLECTION, SKILLS_COLLECTION):
        live = await qdrant_store.alias_target(logical)
        mine = [n for n in names if n == logical or qdrant_store.identity_hash_of(n, logical)]
        lines.append(f"\n{logical}: " + (f"{logical}-live -> {live}" if live else f"no {logical}-live alias"))
        for physical in mine:
            found = qdrant_store.identity_hash_of(physical, logical)
            holds = await qdrant_store.recorded_identity(physical)
            if holds is None and found == expected:
                if record:
                    await qdrant_store.record_identity(physical, configured.describe())
                    holds = configured.describe()
                    what = f"{_describe(holds)} (recorded now)"
                else:
                    what = f"{_describe(configured.describe())} (proved by the name; not recorded, see --record)"
            elif holds is None:
                what = "unknown: built before identities were recorded, under other settings" if found else "unknown: predates identity tracking"
            else:
                what = _describe(holds)
            if physical != live:
                state = "not in use"
            elif found is None:
                state = "IN USE, unverified"
            else:
                state = "IN USE" if found == expected else "IN USE, MISMATCH (retrieval is refused; make reembed)"
            count = (await client.count(collection_name=physical, exact=True)).count
            lines.append(f"  {physical}  [{state}]  {count} points\n    {what}")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.tools.vector_collections", description=__doc__.split("\n\n")[0])
    parser.add_argument("--record", action="store_true", help="write the identity onto a collection whose name proves it")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    wire()
    print("\n".join(asyncio.run(report(record=args.record))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
