"""scripts/tools/qdrant_indexes.py: the payload indexes on a collection made before they existed (PRODUCTION_REVIEW.md R09).

`create_physical` makes them for every collection built from now on; an existing deployment runs this once.
"""
import pytest

from app.adapters.outbound.qdrant import qdrant_store
from scripts.tools import qdrant_indexes
from tests.adapters.outbound.qdrant.fake_qdrant import FakeQdrant
from tests.conftest import TEST_EMBEDDING_IDENTITY

H = TEST_EMBEDDING_IDENTITY.hash8()
FIELDS = {"tenant", "kind", "owner", "topic", "source", "created_at"}


@pytest.fixture
def qdrant(monkeypatch):
    fake = FakeQdrant()
    monkeypatch.setattr(qdrant_store, "get_client", lambda: fake)
    return fake


async def test_the_collection_behind_the_live_alias_gets_every_index(qdrant):
    qdrant.seed(f"docs-{H}")
    qdrant.aliases["docs-live"] = f"docs-{H}"

    lines = await qdrant_indexes.index_collections(["docs"])

    assert lines == [f"docs: indexed docs-{H}"]
    assert set(qdrant.collections[f"docs-{H}"]["indexes"]) == FIELDS


async def test_a_legacy_collection_with_no_alias_is_adopted_first_and_indexed(qdrant):
    qdrant.seed("docs")

    lines = await qdrant_indexes.index_collections(["docs"])

    assert lines == ["docs: indexed docs"]
    assert qdrant.aliases == {"docs-live": "docs"} and set(qdrant.collections["docs"]["indexes"]) == FIELDS


async def test_a_collection_that_does_not_exist_yet_is_not_an_error(qdrant):
    assert await qdrant_indexes.index_collections(["docs"]) == ["docs: no collection yet (run `make ingest`); nothing to index"]


async def test_no_point_is_read_or_written(qdrant):
    qdrant.seed(f"docs-{H}")
    qdrant.aliases["docs-live"] = f"docs-{H}"

    await qdrant_indexes.index_collections(["docs"])

    assert {call[0] for call in qdrant.calls} <= {"create_payload_index", "update_collection_aliases"}


async def test_an_unknown_collection_is_a_usage_error_with_nothing_touched(qdrant, capsys):
    assert await qdrant_indexes._run(["nonsense"]) == qdrant_indexes.EXIT_USAGE
    assert qdrant.calls == [] and "unknown collection" in capsys.readouterr().err


async def test_a_failure_is_reported_by_class_and_leaves_the_collection_as_it_was(qdrant, monkeypatch, capsys):
    qdrant.seed(f"docs-{H}")
    qdrant.aliases["docs-live"] = f"docs-{H}"

    async def broken(physical):
        raise ConnectionError("qdrant went away: http://internal-host:6333")

    monkeypatch.setattr(qdrant_store, "ensure_payload_indexes", broken)

    assert await qdrant_indexes._run(["docs"]) == qdrant_indexes.EXIT_ERROR
    err = capsys.readouterr().err
    assert "ConnectionError" in err and "internal-host" not in err, "the class, never the exception's own text"
