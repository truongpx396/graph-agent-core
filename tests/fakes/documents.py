"""Fakes for `DocumentWriter` (`app/ports/outbound/retrieval.py`) and `ObjectStore` (`app/ports/outbound/storage.py`).

The writer's deletes mean what `qdrant_store.document_filter` means (tenant, `kind == "document"`, source, and for an older-than delete `ingested_at` below the
version or absent), and `tests/integration/test_document_delete_real_qdrant.py` runs the same cases against a real Qdrant: a fake that drifted from the
adapter would pass here and fail there."""
from collections.abc import Sequence
from typing import Any

from app.ports.outbound.retrieval import DocumentPoint


class InMemoryDocumentWriter:
    """Keeps every stored point in `points`, by id. `fail_upsert` / `fail_delete` make the next call of that kind raise, to script a store that is down."""

    def __init__(self) -> None:
        self.points: dict[str, dict[str, Any]] = {}
        self.deletes: list[tuple[str, str, float | None]] = []
        self.fail_upsert: Exception | None = None
        self.fail_delete: Exception | None = None

    async def upsert(self, points: Sequence[DocumentPoint]) -> None:
        if self.fail_upsert is not None:
            raise self.fail_upsert
        for point in points:
            self.points[point.point_id] = dict(point.payload)

    async def delete_older_versions(self, *, tenant: str, source: str, version: float) -> int:
        return self._delete(tenant, source, version)

    async def delete_document(self, *, tenant: str, source: str) -> int:
        return self._delete(tenant, source, None)

    def _delete(self, tenant: str, source: str, older_than: float | None) -> int:
        self.deletes.append((tenant, source, older_than))
        if self.fail_delete is not None:
            raise self.fail_delete
        doomed = [
            point_id
            for point_id, payload in self.points.items()
            if payload.get("tenant") == tenant
            and payload.get("kind") == "document"
            and payload.get("source") == source
            and (older_than is None or not payload.get("ingested_at", float("-inf")) >= older_than)
        ]
        for point_id in doomed:
            del self.points[point_id]
        return len(doomed)

    def ids_of(self, *, tenant: str, source: str) -> set[str]:
        return {i for i, p in self.points.items() if p.get("tenant") == tenant and p.get("source") == source}


class InMemoryObjectStore:
    """Keeps blobs in `objects`. `fail_list` makes `list_keys` raise, to script a store that is down."""

    def __init__(self, keys: Sequence[str] = ()) -> None:
        self.objects: dict[str, bytes] = dict.fromkeys(keys, b"")
        self.fail_list: Exception | None = None

    def upload_bytes(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        self.objects[key] = data

    def delete_object(self, key: str) -> None:
        self.objects.pop(key, None)

    def download_bytes(self, key: str) -> bytes:
        return self.objects[key]

    def list_keys(self, prefix: str) -> list[str]:
        if self.fail_list is not None:
            raise self.fail_list
        return [key for key in self.objects if key.startswith(prefix)]
