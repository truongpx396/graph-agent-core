"""Deleting a document, against a REAL Qdrant (PRODUCTION_REVIEW.md R18).

`tests/fakes/documents.py::InMemoryDocumentWriter` says what a document delete means, and `tests/ingestion/test_ingest_replaces_the_older_version.py` leans on it.
Two things only the server can answer, so they are run here through the real adapter, over a real alias, with the same cases the fake is held to:

- one tenant's delete cannot remove another tenant's points with the same source name (Principle I), nor a memory that happens to carry it;
- "older than this version" really matches a point that has no `ingested_at` at all (written before the field existed): the condition is `NOT ingested_at >=
  version`, and a plain `ingested_at < version` would leave every such point behind for good.

Self-skips (never fails) without Docker, like every `integration` test.
"""
import time
import uuid

import pytest

from app.adapters.outbound.qdrant import qdrant_store
from tests.containers import ensure_qdrant

pytestmark = pytest.mark.integration

VECTOR = [0.1, 0.2, 0.3]
SOURCE = "upload:handbook.pdf"
VERSION = time.time()  # a real epoch-seconds float, as the ingest stamps it


@pytest.fixture
async def live(monkeypatch):
    """A logical collection with a physical one behind its `-live` alias: the path the adapter's deletes take in production."""
    info = ensure_qdrant()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", info["qdrant_url"])
    logical = f"docdelete-{uuid.uuid4().hex[:8]}"
    physical = qdrant_store.physical_name(logical, "abcd1234")
    monkeypatch.setattr(qdrant_store, "COLLECTION", logical)
    qdrant_store.reset_identity_cache()
    await qdrant_store.create_physical(physical, 3)
    await qdrant_store.repoint_live(logical, physical)
    yield physical
    qdrant_store.reset_identity_cache()
    await qdrant_store.get_client().delete_collection(physical)  # its alias goes with it


def _point(name: str, **payload):
    return qdrant_store.build_point(str(uuid.uuid5(uuid.NAMESPACE_URL, name)), VECTOR, {"name": name, "kind": "document", **payload})


async def _names(physical: str) -> set[str]:
    points, _ = await qdrant_store.get_client().scroll(physical, limit=100, with_payload=True)
    return {p.payload["name"] for p in points}


@pytest.fixture
async def seeded(live):
    await qdrant_store.get_client().upsert(
        live,
        [
            _point("mine-1", tenant="acme", source=SOURCE),
            _point("mine-2", tenant="acme", source=SOURCE),
            _point("other-tenant", tenant="globex", source=SOURCE),
            _point("memory", tenant="acme", source=SOURCE, kind="memory"),
            _point("other-file", tenant="acme", source="upload:other.pdf"),
        ],
    )
    return live


async def test_a_delete_removes_one_tenants_document_and_never_another_tenants_or_a_memory(seeded):
    writer = qdrant_store.QdrantDocumentWriter()

    assert await writer.delete_document(tenant="acme", source=SOURCE) == 2

    assert await _names(seeded) == {"other-tenant", "memory", "other-file"}


async def test_the_other_tenants_delete_removes_only_its_own(seeded):
    assert await qdrant_store.QdrantDocumentWriter().delete_document(tenant="globex", source=SOURCE) == 1

    assert await _names(seeded) == {"mine-1", "mine-2", "memory", "other-file"}


async def test_deleting_a_document_that_is_not_there_removes_nothing_and_reports_zero(seeded):
    before = await _names(seeded)

    assert await qdrant_store.QdrantDocumentWriter().delete_document(tenant="initech", source=SOURCE) == 0
    assert await qdrant_store.QdrantDocumentWriter().delete_document(tenant="acme", source="upload:never.pdf") == 0

    assert await _names(seeded) == before


async def test_an_older_than_delete_removes_older_and_unstamped_points_and_keeps_equal_and_newer(live):
    await qdrant_store.get_client().upsert(
        live,
        [
            _point("older", tenant="acme", source=SOURCE, ingested_at=VERSION - 10),
            _point("just-older", tenant="acme", source=SOURCE, ingested_at=VERSION - 0.000001),
            _point("equal", tenant="acme", source=SOURCE, ingested_at=VERSION),
            _point("newer", tenant="acme", source=SOURCE, ingested_at=VERSION + 10),
            _point("unstamped", tenant="acme", source=SOURCE),  # written before the field existed
            _point("other-tenant-older", tenant="globex", source=SOURCE, ingested_at=VERSION - 10),
            _point("memory-older", tenant="acme", source=SOURCE, kind="memory", ingested_at=VERSION - 10),
            _point("other-source-older", tenant="acme", source="upload:other.pdf", ingested_at=VERSION - 10),
        ],
    )

    removed = await qdrant_store.QdrantDocumentWriter().delete_older_versions(tenant="acme", source=SOURCE, version=VERSION)

    assert removed == 3
    assert await _names(live) == {"equal", "newer", "other-tenant-older", "memory-older", "other-source-older"}


async def test_the_source_field_is_indexed_on_the_collection_the_deletes_filter(live):
    info = await qdrant_store.get_client().get_collection(live)

    assert {"tenant", "kind", "source"} <= set(info.payload_schema), "a document delete filters by all three"
