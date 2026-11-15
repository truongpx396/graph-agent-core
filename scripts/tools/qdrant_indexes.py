"""Create the payload indexes a vector search filters by, on a collection that was made before they existed.

    make qdrant-indexes COLLECTION_NAME=docs        # or: python -m scripts.tools.qdrant_indexes [docs] [skills]

`qdrant_store.create_physical` makes them for every collection built from now on (`make ingest`, `make reembed`). An existing deployment runs this
once. It creates `tenant` (marked `is_tenant`), `kind`, `owner`, `topic`, `source` and `created_at` indexes on the collection behind each logical
name; creating one that exists is not an error, and creating one on a live collection is an online operation, so it is safe to repeat and to run
while serving. Nothing is read from or written to a point (PRODUCTION_REVIEW.md R09).

EXIT CODES. 0 every named collection is indexed (or does not exist yet: nothing to do) | 1 error | 2 usage.
"""
import argparse
import asyncio
import sys

from app.adapters.outbound.qdrant import qdrant_store
from app.bootstrap import wire
from app.core.config import COLLECTION, SKILLS_COLLECTION

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2


async def index_collections(names: list[str]) -> list[str]:
    """The lines to print, one per collection."""
    lines = []
    for logical in names:
        physical = await qdrant_store.ensure_live(logical)
        if physical is None:
            lines.append(f"{logical}: no collection yet (run `make ingest`); nothing to index")
            continue
        await qdrant_store.ensure_payload_indexes(physical)
        lines.append(f"{logical}: indexed {physical}")
    return lines


async def _run(names: list[str]) -> int:
    unknown = [name for name in names if name not in (COLLECTION, SKILLS_COLLECTION)]
    if unknown:
        print(f"unknown collection {unknown[0]!r}; expected {COLLECTION!r} or {SKILLS_COLLECTION!r}", file=sys.stderr)
        return EXIT_USAGE
    try:
        print("\n".join(await index_collections(names)))
    except Exception as exc:  # noqa: BLE001 - an operator script: report the class and leave the collection as it was
        print(f"failed ({type(exc).__name__}): the collection is unchanged", file=sys.stderr)
        return EXIT_ERROR
    finally:
        await qdrant_store.close_client()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("collections", nargs="*", help=f"logical names; default {COLLECTION} and {SKILLS_COLLECTION}")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    wire()
    return asyncio.run(_run(args.collections or [COLLECTION, SKILLS_COLLECTION]))


if __name__ == "__main__":
    raise SystemExit(main())
