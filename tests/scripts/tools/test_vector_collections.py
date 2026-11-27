"""scripts/tools/vector_collections.py: which model each collection holds, in words, and a record written only where the name proves it."""
import pytest

from app.adapters.outbound.embeddings import embedding_identity
from app.adapters.outbound.qdrant import qdrant_store
from scripts.tools import vector_collections
from tests.adapters.outbound.qdrant.fake_qdrant import FakeQdrant
from tests.conftest import TEST_EMBEDDING_IDENTITY

H = TEST_EMBEDDING_IDENTITY.hash8()
OTHER = "ffffffff"


@pytest.fixture
def world(monkeypatch):
    """`docs` was re-embedded: the live collection is current and the one it replaced is still there. `skills` was built under other settings."""
    fake = FakeQdrant()
    for name in (f"docs-{H}", f"docs-{OTHER}", f"skills-{OTHER}", "unrelated"):
        fake.seed(name)
    fake.aliases = {"docs-live": f"docs-{H}", "skills-live": f"skills-{OTHER}"}
    monkeypatch.setattr(qdrant_store, "get_client", lambda: fake)

    async def configured():
        return TEST_EMBEDDING_IDENTITY

    monkeypatch.setattr(embedding_identity, "current_identity", configured)
    return fake


def _entry(lines, physical):
    """A collection's two lines: its name and state, then what it holds."""
    text = "\n".join(lines).split("\n")
    at = next(i for i, line in enumerate(text) if line.startswith(f"  {physical}  "))
    return "\n".join(text[at : at + 2])


async def test_each_collection_is_listed_with_whether_it_is_in_use_and_what_it_holds(world):
    lines = await vector_collections.report()

    assert TEST_EMBEDDING_IDENTITY.resolved_model in lines[0]
    assert "[IN USE]" in _entry(lines, f"docs-{H}") and TEST_EMBEDDING_IDENTITY.resolved_model in _entry(lines, f"docs-{H}")
    assert "[not in use]" in _entry(lines, f"docs-{OTHER}") and "unknown" in _entry(lines, f"docs-{OTHER}")
    assert "MISMATCH" in _entry(lines, f"skills-{OTHER}")
    assert "unrelated" not in "\n".join(lines)


async def test_it_writes_nothing_unless_asked(world):
    await vector_collections.report()

    assert await qdrant_store.recorded_identity(f"docs-{H}") is None


async def test_record_writes_only_where_the_name_proves_what_the_collection_holds(world):
    await vector_collections.report(record=True)

    assert await qdrant_store.recorded_identity(f"docs-{H}") == TEST_EMBEDDING_IDENTITY.describe()
    assert await qdrant_store.recorded_identity(f"docs-{OTHER}") is None
    assert await qdrant_store.recorded_identity(f"skills-{OTHER}") is None


async def test_a_recorded_collection_still_says_what_it_holds_after_the_settings_move_on(world):
    await qdrant_store.record_identity(f"docs-{OTHER}", {"model": "ollama/old-model", "dim": 384, "query_prefix": "q: "})

    entry = _entry(await vector_collections.report(), f"docs-{OTHER}")

    assert "ollama/old-model, 384 dimensions, query prefix 'q: '" in entry
