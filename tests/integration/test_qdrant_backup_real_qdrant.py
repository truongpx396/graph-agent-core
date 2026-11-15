"""A Qdrant backup restored into a DIFFERENT, fresh server (PRODUCTION_REVIEW.md R26).

A backup nobody has restored is a hope. This takes snapshots from one real Qdrant and recovers them into a second one that has never seen the data, which is what a
rebuilt droplet looks like, and checks what the app depends on: the points, a search over them, the payload indexes every filtered search needs (R09), and the ALIAS
the app reads and writes through, which a snapshot does not carry.

Self-skips (never fails) without Docker, like every `integration` test."""
import time
import uuid

import pytest

from app.adapters.outbound.qdrant import qdrant_store
from scripts.tools import qdrant_backup
from tests.containers import _require_docker, _shipped_image

pytestmark = pytest.mark.integration

VECTOR_SIZE = 3


def _start_qdrant():
    from qdrant_client import QdrantClient
    from testcontainers.core.container import DockerContainer

    container = DockerContainer(_shipped_image("qdrant")).with_exposed_ports(6333)
    container.start()
    url = f"http://{container.get_container_host_ip()}:{int(container.get_exposed_port(6333))}"
    for attempt in range(40):
        try:
            QdrantClient(url=url).get_collections()
            return container, url
        except Exception:  # retry until it answers
            if attempt == 39:
                container.stop()
                raise
            time.sleep(1)
    raise AssertionError("unreachable")


@pytest.fixture
def servers():
    _require_docker("two real Qdrant servers")
    source, source_url = _start_qdrant()
    target, target_url = _start_qdrant()
    try:
        yield source_url, target_url
    finally:
        source.stop()
        target.stop()


async def _use(monkeypatch, url: str) -> None:
    """Points the app's Qdrant client, and the tool's own URL for the snapshot download, at `url`."""
    await qdrant_store.close_client()
    monkeypatch.setattr(qdrant_store, "QDRANT_URL", url)
    monkeypatch.setattr(qdrant_backup, "QDRANT_URL", url)
    qdrant_store.reset_identity_cache()


def _points(n: int, **payload):
    return [
        qdrant_store.build_point(str(uuid.uuid5(uuid.NAMESPACE_URL, f"p-{i}")), [i / n, 1 - i / n, 0.5], {"n": i, "tenant": "acme", "kind": "document", **payload})
        for i in range(n)
    ]


async def _seed(monkeypatch, url: str) -> None:
    await _use(monkeypatch, url)
    await qdrant_store.create_physical("docs-abcd1234", VECTOR_SIZE)  # with its payload indexes, as the app makes it
    await qdrant_store.create_physical("skills-abcd1234", VECTOR_SIZE)
    await qdrant_store.get_client().upsert("docs-abcd1234", _points(50))
    await qdrant_store.get_client().upsert("skills-abcd1234", _points(5, kind="skill"))
    await qdrant_store.repoint_live("docs", "docs-abcd1234")  # the alias the app reads and writes through


async def _top3(alias: str) -> list[str]:
    response = await qdrant_store.get_client().query_points(collection_name=alias, query=[0.3, 0.7, 0.5], using=qdrant_store.DENSE_VECTOR_NAME, limit=3)
    return [str(point.id) for point in response.points]


async def test_a_backup_restored_into_a_fresh_server_has_the_points_the_search_the_indexes_and_the_alias(servers, tmp_path, monkeypatch):
    source_url, target_url = servers
    await _seed(monkeypatch, source_url)
    expected_top3 = await _top3("docs-live")

    lines = await qdrant_backup.backup(tmp_path)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["qdrant-aliases.json", "qdrant-docs-abcd1234.snapshot", "qdrant-skills-abcd1234.snapshot"]
    assert any(line.startswith("docs-abcd1234:") for line in lines)
    for name in ("docs-abcd1234", "skills-abcd1234"):
        assert await qdrant_store.get_client().list_snapshots(name) == [], "the server's own copy is deleted once downloaded, so backups do not fill its disk"

    await _use(monkeypatch, target_url)
    assert (await qdrant_store.get_client().get_collections()).collections == [], "the target has never seen this data"
    await qdrant_backup.restore(tmp_path)
    client = qdrant_store.get_client()

    assert (await client.count("docs-abcd1234", exact=True)).count == 50
    assert (await client.count("skills-abcd1234", exact=True)).count == 5
    assert await _top3("docs-live") == expected_top3, "the same query returns the same points through the restored alias"
    assert await qdrant_store.alias_target("docs") == "docs-abcd1234"
    schema = (await client.get_collection("docs-abcd1234")).payload_schema
    assert {"tenant", "kind", "source", "created_at"} <= set(schema), "the payload indexes R09 added came back with the collection"
    filtered = await client.count("docs-abcd1234", count_filter=qdrant_store.document_filter("acme", "x"), exact=True)
    assert filtered.count == 0 and (await client.count("docs-abcd1234", exact=True)).count == 50


async def test_restoring_over_an_existing_collection_is_refused_until_overwrite_is_asked_for(servers, tmp_path, monkeypatch):
    source_url, target_url = servers
    await _seed(monkeypatch, source_url)
    await qdrant_backup.backup(tmp_path)
    await _use(monkeypatch, target_url)
    await qdrant_backup.restore(tmp_path)
    await qdrant_store.get_client().upsert("docs-abcd1234", _points(1, source="added-after"))  # a change a careless restore would erase

    with pytest.raises(FileExistsError, match="docs-abcd1234"):
        await qdrant_backup.restore(tmp_path)
    assert (await qdrant_store.get_client().count("docs-abcd1234", exact=True)).count == 50, "nothing was touched by the refused restore"

    await qdrant_backup.restore(tmp_path, overwrite=True)
    assert (await qdrant_store.get_client().count("docs-abcd1234", exact=True)).count == 50


async def test_restoring_from_a_directory_with_no_snapshots_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        await qdrant_backup.restore(tmp_path)


async def test_a_server_with_no_collections_backs_up_to_just_an_empty_alias_list(servers, tmp_path, monkeypatch):
    source_url, _ = servers
    await _use(monkeypatch, source_url)

    lines = await qdrant_backup.backup(tmp_path)

    assert lines == ["aliases: 0"] and [p.name for p in tmp_path.iterdir()] == ["qdrant-aliases.json"]
