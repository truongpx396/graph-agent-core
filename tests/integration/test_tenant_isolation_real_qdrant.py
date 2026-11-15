"""Principle I's Qdrant half, against a REAL Qdrant: `scope_filter`'s translation of an `AccessScope` into a pre-filter.

`qdrant_store.scope_filter`'s docstring has long said this file "proves the result against a real Qdrant". It did not exist: the translation tenant
isolation rests on was pinned only against a fake (tests/adapters/outbound/qdrant/test_qdrant_store.py), which can drift from the server it imitates
(PRODUCTION_REVIEW.md R09, R21). No model is needed: two tenants and two owners with fixed small vectors, and a query that carries `scope_filter`'s result.

Self-skips (never fails) without Docker, like every `integration` test.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.adapters.outbound.qdrant import qdrant_store
from app.core.security import AccessScope
from tests.containers import ensure_qdrant

pytestmark = pytest.mark.integration

VECTOR = [0.1, 0.2, 0.3]  # every point is equally near the query: only the FILTER decides what comes back
NOW = datetime.now(UTC)


@pytest.fixture
async def collection(monkeypatch):
    info = ensure_qdrant()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", info["qdrant_url"])
    name = f"isolation-{uuid.uuid4().hex[:10]}"
    await qdrant_store.create_physical(name, 3)
    yield name
    await qdrant_store.get_client().delete_collection(name)


def _point(n: int, **payload):
    return qdrant_store.build_point(str(uuid.uuid5(uuid.NAMESPACE_URL, f"point-{n}")), VECTOR, {"text": f"point {n}", **payload})


async def _seen(collection, scope: AccessScope) -> set[str]:
    response = await qdrant_store.get_client().query_points(
        collection_name=collection, query=VECTOR, using=qdrant_store.DENSE_VECTOR_NAME, query_filter=qdrant_store.scope_filter(scope), limit=50
    )
    return {point.payload["text"] for point in response.points}


@pytest.fixture
async def seeded(collection):
    recent = (NOW - timedelta(days=1)).isoformat()
    points = [
        _point(1, tenant="acme", kind="document"),
        _point(2, tenant="acme", kind="document"),
        _point(3, tenant="globex", kind="document"),
        _point(4, tenant="acme", kind="memory", owner="alice", created_at=recent),
        _point(5, tenant="acme", kind="memory", owner="bob", created_at=recent),
        _point(6, tenant="globex", kind="memory", owner="alice", created_at=recent),
        _point(7, tenant="acme", kind="memory", owner="alice"),  # written before `created_at` existed
        _point(8, tenant="acme", kind="memory", owner="alice", created_at=(NOW - timedelta(days=400)).isoformat()),
    ]
    await qdrant_store.get_client().upsert(collection, points)
    return collection


async def test_a_tenant_sees_only_its_own_documents(seeded):
    assert await _seen(seeded, AccessScope(tenant="acme", kind="document")) == {"point 1", "point 2"}
    assert await _seen(seeded, AccessScope(tenant="globex", kind="document")) == {"point 3"}


async def test_a_tenant_with_nothing_sees_nothing_and_never_another_tenants_points(seeded):
    assert await _seen(seeded, AccessScope(tenant="initech", kind="document")) == set()


async def test_a_document_scope_never_returns_a_memory_and_a_memory_scope_never_a_document(seeded):
    assert {"point 4", "point 5"}.isdisjoint(await _seen(seeded, AccessScope(tenant="acme", kind="document")))
    assert {"point 1", "point 2"}.isdisjoint(await _seen(seeded, AccessScope(tenant="acme", kind="memory", owner="alice")))


async def test_a_memory_scope_sees_only_its_owners_memories_in_its_own_tenant(seeded):
    cutoff = NOW - timedelta(days=365)

    assert await _seen(seeded, AccessScope(tenant="acme", kind="memory", owner="alice", created_after=cutoff)) == {"point 4"}
    assert await _seen(seeded, AccessScope(tenant="acme", kind="memory", owner="bob", created_after=cutoff)) == {"point 5"}
    assert await _seen(seeded, AccessScope(tenant="globex", kind="memory", owner="alice", created_after=cutoff)) == {"point 6"}


async def test_a_point_with_no_created_at_is_excluded_by_a_retention_cutoff_and_so_is_an_old_one(seeded):
    """Qdrant never satisfies a range condition with a missing field: a memory written before the field existed is invisible, not visible for ever."""
    cutoff = NOW - timedelta(days=365)

    without_cutoff = await _seen(seeded, AccessScope(tenant="acme", kind="memory", owner="alice"))
    with_cutoff = await _seen(seeded, AccessScope(tenant="acme", kind="memory", owner="alice", created_after=cutoff))

    assert {"point 4", "point 7", "point 8"} <= without_cutoff, "control: with no cutoff they are all there"
    assert with_cutoff == {"point 4"}


async def test_the_collection_carries_the_payload_indexes_the_filters_use(collection):
    info = await qdrant_store.get_client().get_collection(collection)

    assert {"tenant", "kind", "owner", "topic", "source", "created_at"} <= set(info.payload_schema)
    assert info.payload_schema["tenant"].params.is_tenant is True
    assert str(info.payload_schema["created_at"].data_type.value) == "datetime"


async def test_the_index_function_is_safe_to_run_again_on_a_live_collection(collection):
    await qdrant_store.ensure_payload_indexes(collection)
    await qdrant_store.ensure_payload_indexes(collection)

    assert "tenant" in (await qdrant_store.get_client().get_collection(collection)).payload_schema
