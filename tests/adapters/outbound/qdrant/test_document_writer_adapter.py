"""`QdrantDocumentWriter` (`app/adapters/outbound/qdrant/qdrant_store.py`), the `DocumentWriter` port (`app/ports/outbound/retrieval.py`) the ingest pipeline reaches."""
import pytest
from qdrant_client.models import Filter, Range

from app.adapters.outbound.qdrant import qdrant_store
from app.ports.outbound.retrieval import DocumentPoint


async def test_the_writer_stores_each_point_by_its_id_with_its_vectors_and_payload(monkeypatch):
    written = []

    async def upsert(points):
        written.extend(points)

    monkeypatch.setattr(qdrant_store, "upsert", upsert)

    await qdrant_store.QdrantDocumentWriter().upsert(
        [
            DocumentPoint("00000000-0000-0000-0000-000000000001", [0.1, 0.2], {"tenant": "acme", "text": "t"}, ([3], [0.5])),
            DocumentPoint("00000000-0000-0000-0000-000000000002", [0.3, 0.4], {"tenant": "acme", "text": "u"}),
        ]
    )

    assert [p.id for p in written] == ["00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"]
    assert [p.payload["tenant"] for p in written] == ["acme", "acme"]
    assert written[0].vector == qdrant_store.build_point("x", [0.1, 0.2], {}, ([3], [0.5])).vector
    assert written[1].vector == qdrant_store.build_point("x", [0.3, 0.4], {}).vector


# --- Deleting a document (PRODUCTION_REVIEW.md R18) ---------------------------------------------------------------------


def _conditions(f: Filter):
    return {c.key: c for c in (f.must or [])}, {c.key: c for c in (f.must_not or [])}


def test_the_document_filter_always_carries_the_tenant_the_document_kind_and_the_source():
    must, must_not = _conditions(qdrant_store.document_filter("acme", "upload:a.pdf"))

    assert {k: c.match.value for k, c in must.items()} == {"tenant": "acme", "kind": "document", "source": "upload:a.pdf"}
    assert must_not == {}


def test_an_older_than_filter_excludes_what_is_at_or_above_the_version_so_a_missing_stamp_counts_as_older():
    """`NOT ingested_at >= version` matches a point with no `ingested_at` (written before the field existed); `ingested_at < version` would not."""
    must, must_not = _conditions(qdrant_store.document_filter("acme", "upload:a.pdf", older_than=150.0))

    assert set(must) == {"tenant", "kind", "source"}, "the version never replaces a scope condition"
    assert list(must_not) == ["ingested_at"] and must_not["ingested_at"].range == Range(gte=150.0)


@pytest.mark.parametrize("count", [3, 0])
async def test_a_delete_counts_then_deletes_and_does_not_delete_when_there_is_nothing(monkeypatch, count):
    calls = []

    async def count_by_filter(f):
        calls.append(("count", f))
        return count

    async def delete_by_filter(f):
        calls.append(("delete", f))

    monkeypatch.setattr(qdrant_store, "count_by_filter", count_by_filter)
    monkeypatch.setattr(qdrant_store, "delete_by_filter", delete_by_filter)

    assert await qdrant_store.QdrantDocumentWriter().delete_document(tenant="acme", source="upload:a.pdf") == count

    assert [name for name, _ in calls] == (["count", "delete"] if count else ["count"])
    counted = calls[0][1]
    assert all(call_filter == counted for _, call_filter in calls), "what is counted is exactly what is deleted"
    assert counted == qdrant_store.document_filter("acme", "upload:a.pdf")


async def test_deleting_older_versions_passes_the_version_through_to_the_filter(monkeypatch):
    seen = []

    async def count_by_filter(f):
        seen.append(f)
        return 1

    async def delete_by_filter(f):
        seen.append(f)

    monkeypatch.setattr(qdrant_store, "count_by_filter", count_by_filter)
    monkeypatch.setattr(qdrant_store, "delete_by_filter", delete_by_filter)

    assert await qdrant_store.QdrantDocumentWriter().delete_older_versions(tenant="acme", source="upload:a.pdf", version=42.0) == 1

    assert seen == [qdrant_store.document_filter("acme", "upload:a.pdf", older_than=42.0)] * 2


def test_the_source_field_has_a_payload_index_because_every_document_delete_filters_by_it():
    import inspect

    assert '"source"' in inspect.getsource(qdrant_store.ensure_payload_indexes)
