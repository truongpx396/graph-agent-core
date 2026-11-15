"""PRODUCTION_REVIEW.md R18: an edited document that is uploaded again stops answering with its old text.

A chunk's point id is derived from its text, so a changed chunk got a NEW id and the old one stayed, retrievable and citable. An ingest that is given an
`ingest_id` now writes its chunks under that id and, once they are all stored, removes the same source's chunks from OLDER ingests.

The cases that matter are the ones a plain "delete whatever has another ingest id" gets wrong: two uploads of one name running at once (each would delete the
other's writes, so a double-submitted upload could leave NO document), a job a crash made run again after a newer upload, and many documents that share one
source (`scripts/tools/seed.py`)."""
import logging
from pathlib import Path

import pytest

from app.adapters.outbound.embeddings import embeddings
from app.ingestion import ingestor
from app.ports.outbound.retrieval import document_writer
from tests.conftest import TEST_CTX, metric_value
from tests.fakes.documents import InMemoryDocumentWriter

SOURCE = "upload:handbook.md"
OTHER_TENANT_CTX = {**TEST_CTX, "tenant": "globex"}


def _text(word: str, paragraphs: int = 12) -> str:
    """Enough text for several chunks, every paragraph different so no two chunks are alike."""
    return "\n\n".join(f"{word} paragraph {n}. " + (f"{word} detail {n} " * 40) for n in range(paragraphs))


@pytest.fixture(autouse=True)
def embedding_calls(monkeypatch):
    async def embed_documents(texts):
        return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(embeddings, "embed_documents", embed_documents)
    monkeypatch.setattr(embeddings, "embed_sparse_batch", lambda texts: [([1], [0.5]) for _ in texts])


@pytest.fixture
def writer():
    with document_writer.overriding(InMemoryDocumentWriter()) as fake:
        yield fake


async def _ingest(text, *, ingest_id, queued_at, ctx=TEST_CTX, source=SOURCE):
    return await ingestor.ingest_text(text, title="Handbook", ctx=ctx, source=source, ingest_id=ingest_id, queued_at=queued_at)


def _mine(writer, ctx=TEST_CTX, source=SOURCE):
    return writer.ids_of(tenant=ctx["tenant"], source=source)


async def test_re_ingesting_a_changed_document_leaves_exactly_the_new_chunks(writer):
    await _ingest(_text("old"), ingest_id="job-1", queued_at=100.0)
    first_ids = _mine(writer)
    assert first_ids

    await _ingest(_text("new"), ingest_id="job-2", queued_at=200.0)

    second = {i for i, p in writer.points.items() if p["ingest_id"] == "job-2"}
    assert _mine(writer) == second and second, "only the new version is left"
    assert not first_ids & _mine(writer)
    assert all("old" not in p["text"] for p in writer.points.values())


async def test_re_ingesting_identical_text_leaves_one_copy_not_two(writer):
    await _ingest(_text("same"), ingest_id="job-1", queued_at=100.0)
    count = len(_mine(writer))

    await _ingest(_text("same"), ingest_id="job-2", queued_at=200.0)

    assert len(_mine(writer)) == count
    assert {p["ingest_id"] for p in writer.points.values()} == {"job-2"}


async def test_a_document_that_became_shorter_loses_the_chunks_it_no_longer_has(writer):
    await _ingest(_text("long", paragraphs=20), ingest_id="job-1", queued_at=100.0)
    before = len(_mine(writer))

    await _ingest(_text("long", paragraphs=3), ingest_id="job-2", queued_at=200.0)

    assert 0 < len(_mine(writer)) < before


async def test_a_failed_upsert_leaves_the_old_version_serving(writer):
    await _ingest(_text("old"), ingest_id="job-1", queued_at=100.0)
    old_ids = _mine(writer)
    deletes_so_far = list(writer.deletes)
    writer.fail_upsert = RuntimeError("qdrant is down")

    with pytest.raises(RuntimeError):
        await _ingest(_text("new"), ingest_id="job-2", queued_at=200.0)

    assert _mine(writer) == old_ids
    assert writer.deletes == deletes_so_far, "nothing is removed before the new version is stored"


async def test_a_failed_embedding_leaves_the_old_version_serving(writer, monkeypatch):
    await _ingest(_text("old"), ingest_id="job-1", queued_at=100.0)
    old_ids = _mine(writer)
    deletes_so_far = list(writer.deletes)

    async def embed_documents(texts):
        raise RuntimeError("embedding model is down")

    monkeypatch.setattr(embeddings, "embed_documents", embed_documents)

    with pytest.raises(RuntimeError):
        await _ingest(_text("new"), ingest_id="job-2", queued_at=200.0)

    assert _mine(writer) == old_ids and writer.deletes == deletes_so_far


async def test_an_ingest_with_no_id_only_adds_so_many_documents_can_share_one_source(writer):
    """`scripts/tools/seed.py` ingests every sample document under the one source `sample_docs`: none may replace another."""
    for title, word in (("a", "alpha"), ("b", "beta"), ("c", "gamma")):
        await ingestor.ingest_text(_text(word, 3), title=title, ctx=TEST_CTX, source="sample_docs")

    assert {p["title"] for p in writer.points.values()} == {"a", "b", "c"}, "every document is still there"
    assert writer.deletes == []
    assert all("ingest_id" not in p and "ingested_at" not in p for p in writer.points.values())


async def test_without_an_id_the_ids_are_still_derived_from_the_content_alone(writer):
    await ingestor.ingest_text(_text("same", 3), title="t", ctx=TEST_CTX, source="sample_docs")
    first = set(writer.points)
    await ingestor.ingest_text(_text("same", 3), title="t", ctx=TEST_CTX, source="sample_docs")

    assert set(writer.points) == first, "a repeated ingest upserts onto the same ids, as before"


async def test_another_source_and_another_tenant_are_left_alone(writer):
    await _ingest(_text("x"), ingest_id="a", queued_at=100.0, source="upload:other.md")
    await _ingest(_text("x"), ingest_id="b", queued_at=100.0, ctx=OTHER_TENANT_CTX)
    others = set(writer.points)

    await _ingest(_text("new"), ingest_id="c", queued_at=200.0)

    assert others <= set(writer.points), "a re-upload of one name removes only that tenant's chunks of that name"


async def test_the_ids_of_two_ingests_of_identical_text_differ_so_neither_can_overwrite_the_other(writer):
    """The second ingest is the OLDER one, so it removes nothing and both stay: with ids shared between the ingests there would be one set of points, stamped
    by whichever wrote last, and the newer ingest's cleanup would then delete chunks of its own version."""
    await _ingest(_text("same"), ingest_id="newer", queued_at=200.0)
    count = len(writer.points)

    await _ingest(_text("same"), ingest_id="older", queued_at=100.0)

    assert len(writer.points) == 2 * count
    assert {p["ingest_id"] for p in writer.points.values()} == {"newer", "older"}


async def test_the_same_job_run_again_lands_on_the_same_ids(writer):
    """A reclaimed job keeps its id, so a crash halfway through does not leave two copies."""
    await _ingest(_text("same"), ingest_id="job-1", queued_at=100.0)
    first = set(writer.points)

    await _ingest(_text("same"), ingest_id="job-1", queued_at=100.0)

    assert set(writer.points) == first


@pytest.mark.parametrize("same_text", [True, False], ids=["identical text", "different text"])
async def test_two_uploads_of_one_name_that_overlap_in_time_never_leave_no_document(writer, same_text):
    """The newer upload's chunks are written FIRST and the older upload then finishes: its cleanup removes only chunks older than itself, so the newer
    version survives. (Deleting 'whatever has another ingest id' would remove it, and the newer upload's own cleanup would remove the older: no document.)"""
    older_text, newer_text = _text("same"), _text("same" if same_text else "newer")
    await _ingest(newer_text, ingest_id="newer", queued_at=200.0)
    await _ingest(older_text, ingest_id="older", queued_at=100.0)

    survivors = {p["ingest_id"] for p in writer.points.values()}
    assert "newer" in survivors, "the newer upload is never removed by an older one"


async def test_the_older_upload_that_finishes_last_leaves_its_chunks_until_the_next_upload_and_the_next_upload_removes_them(writer):
    """The disclosed limit: an older job finishing after a newer one has already cleaned up has nothing newer to remove it, so its chunks stay. That is what every
    re-upload did before R18, never worse, and the next upload of the name removes both."""
    await _ingest(_text("newer"), ingest_id="newer", queued_at=200.0)
    await _ingest(_text("older"), ingest_id="older", queued_at=100.0)
    assert {p["ingest_id"] for p in writer.points.values()} == {"newer", "older"}

    await _ingest(_text("third"), ingest_id="third", queued_at=300.0)

    assert {p["ingest_id"] for p in writer.points.values()} == {"third"}


async def test_a_job_run_again_after_a_crash_is_ordered_by_when_it_was_queued_not_when_it_ran(writer):
    """Job 1 was queued first, crashed and is retried after job 2 (queued later) finished: the retry must not remove job 2's chunks."""
    await _ingest(_text("second"), ingest_id="job-2", queued_at=200.0)

    await _ingest(_text("first"), ingest_id="job-1", queued_at=100.0)

    assert any(p["ingest_id"] == "job-2" for p in writer.points.values())


async def test_chunks_from_before_the_version_field_existed_are_replaced_by_the_first_re_upload(writer):
    """Points ingested by a release that did not stamp a version have no `ingested_at`: they count as older, or they would stay for good."""
    writer.points["legacy-1"] = {"tenant": TEST_CTX["tenant"], "kind": "document", "source": SOURCE, "text": "legacy"}

    await _ingest(_text("new"), ingest_id="job-1", queued_at=100.0)

    assert "legacy-1" not in writer.points


async def test_a_note_or_memory_with_the_same_source_name_is_never_removed(writer):
    writer.points["memory-1"] = {"tenant": TEST_CTX["tenant"], "kind": "memory", "source": SOURCE, "text": "remembered"}

    await _ingest(_text("new"), ingest_id="job-1", queued_at=100.0)

    assert "memory-1" in writer.points


async def test_a_cleanup_that_fails_is_counted_and_logged_and_does_not_fail_the_ingest(writer, caplog):
    await _ingest(_text("old"), ingest_id="job-1", queued_at=100.0)
    writer.fail_delete = RuntimeError("qdrant refused the delete")
    before = metric_value(ingestor.metrics.agent_ingest_cleanup_failed_total)

    with caplog.at_level(logging.WARNING, logger=ingestor.logger.name):
        written = await _ingest(_text("new"), ingest_id="job-2", queued_at=200.0)

    assert written > 0, "the new version is stored, so the job succeeds"
    assert metric_value(ingestor.metrics.agent_ingest_cleanup_failed_total) == before + 1
    assert any(r.message == "ingest_cleanup_failed" for r in caplog.records)
    assert {p["ingest_id"] for p in writer.points.values()} == {"job-1", "job-2"}, "both versions answer until a later ingest retries it"


async def test_the_next_ingest_removes_what_a_failed_cleanup_left(writer):
    await _ingest(_text("old"), ingest_id="job-1", queued_at=100.0)
    writer.fail_delete = RuntimeError("down")
    await _ingest(_text("new"), ingest_id="job-2", queued_at=200.0)
    writer.fail_delete = None

    await _ingest(_text("newest"), ingest_id="job-3", queued_at=300.0)

    assert {p["ingest_id"] for p in writer.points.values()} == {"job-3"}


async def test_the_default_version_is_now_so_a_direct_ingest_replaces_the_previous_one(writer, monkeypatch):
    monkeypatch.setattr(ingestor.time, "time", lambda: 1000.0)
    await ingestor.ingest_text(_text("old"), title="t", ctx=TEST_CTX, source=SOURCE, ingest_id="a")
    monkeypatch.setattr(ingestor.time, "time", lambda: 2000.0)
    await ingestor.ingest_text(_text("new"), title="t", ctx=TEST_CTX, source=SOURCE, ingest_id="b")

    assert {p["ingest_id"] for p in writer.points.values()} == {"b"}
    assert {p["ingested_at"] for p in writer.points.values()} == {2000.0}


async def test_ingest_file_replaces_a_changed_file_of_the_same_name(writer, tmp_path: Path):
    path = tmp_path / "policy.md"
    path.write_text(_text("old"), encoding="utf-8")
    await ingestor.ingest_file(str(path), TEST_CTX)
    path.write_text(_text("new"), encoding="utf-8")
    await ingestor.ingest_file(str(path), TEST_CTX)

    assert all("old" not in p["text"] for p in writer.points.values()) and writer.points


async def test_an_ingest_that_writes_nothing_removes_nothing(writer):
    """Blank text produces no chunks and returns before the upsert: it must not delete the previous version of the document."""
    await _ingest(_text("old"), ingest_id="job-1", queued_at=100.0)
    kept = set(writer.points)

    assert await _ingest("   ", ingest_id="job-2", queued_at=200.0) == 0

    assert set(writer.points) == kept
