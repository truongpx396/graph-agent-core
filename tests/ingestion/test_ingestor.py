"""Tests for app/ingestion/ingestor.py — mocks embed_texts/embed_sparse_batch/
qdrant_store.upsert (same boundary tests/agent/tools/test_tools.py mocks for the
single-item embed_text/embed_sparse), plus socket.getaddrinfo/httpx for
ingest_url, so these stay hermetic (no live network, no live Qdrant) like the
rest of the suite.

`ingest_text`/`ingest_file`/`ingest_url` are `async def` now (they await
`embed_texts`/`qdrant_store.upsert`, both real I/O against
`AsyncOpenAI`/`AsyncQdrantClient` clients now), so every call below runs
through `asyncio.run(...)`, this repo's established pattern for exercising
async code from a plain `def test_...`. `embed_sparse_batch` stays a plain
sync mock — that leg is genuinely local ONNX compute, unchanged.
"""

import socket
import threading
from pathlib import Path

import pytest

from app.adapters.outbound.embeddings import embeddings
from app.adapters.outbound.qdrant import qdrant_store
from app.ingestion import ingestor
from app.ports.outbound.web import FetchedPage, PageFetchFailedError, page_fetcher
from tests.conftest import TEST_CTX, metric_value


def _mock_embeddings(monkeypatch):
    async def fake_embed_texts(texts):
        return [[0.1, 0.2] for _ in texts]

    monkeypatch.setattr(embeddings, "embed_documents", fake_embed_texts)
    monkeypatch.setattr(
        embeddings, "embed_sparse_batch", lambda texts: [([1, 2], [0.5, 0.5]) for _ in texts]
    )


def _mock_upsert(monkeypatch, captured, key="points"):
    async def fake_upsert(points):
        captured[key] = points

    monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)


class TestIngestText:
    async def test_refuses_without_ctx(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor.ingest_text("some content", title="T", ctx=None)

    async def test_blank_text_writes_nothing_and_is_not_an_error(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = []

        async def fake_upsert(points):
            captured.append(points)

        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        count = await ingestor.ingest_text("   ", title="T", ctx=TEST_CTX)

        assert count == 0
        assert captured == []

    async def test_writes_one_point_per_child_chunk_with_parent_fields(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = {}
        _mock_upsert(monkeypatch, captured)

        text = "Paragraph one about checkpointers.\n\nParagraph two about Qdrant."
        count = await ingestor.ingest_text(text, title="My Doc", ctx=TEST_CTX, source="text", topic="langgraph")

        assert count == len(captured["points"])
        assert count >= 1
        for point in captured["points"]:
            assert point.payload["tenant"] == TEST_CTX["tenant"]
            assert point.payload["ingested_by"] == TEST_CTX["principal"]
            assert point.payload["kind"] == "document"
            assert point.payload["title"] == "My Doc"
            assert point.payload["topic"] == "langgraph"
            assert "parent_id" in point.payload
            assert "parent_text" in point.payload

    async def test_reingesting_identical_content_produces_the_same_point_ids(self, monkeypatch):
        """The actual fix this proves: a retried/reclaimed ingest job, or a
        client's double-submitted upload, re-runs ingest_text with the
        SAME text/source/ctx — point ids must land on the exact same
        values both times so qdrant_store.upsert overwrites rather than
        duplicates. See ingestor.py::_content_point_id's own docstring."""
        _mock_embeddings(monkeypatch)
        text = "Paragraph one about checkpointers.\n\nParagraph two about Qdrant."

        first = {}
        _mock_upsert(monkeypatch, first)
        await ingestor.ingest_text(text, title="My Doc", ctx=TEST_CTX, source="upload:report.pdf")

        second = {}
        _mock_upsert(monkeypatch, second)
        await ingestor.ingest_text(text, title="My Doc", ctx=TEST_CTX, source="upload:report.pdf")

        first_ids = [p.id for p in first["points"]]
        second_ids = [p.id for p in second["points"]]
        assert first_ids == second_ids
        assert len(set(first_ids)) == len(first_ids)  # no accidental collision between DIFFERENT chunks

    async def test_different_content_produces_different_point_ids(self, monkeypatch):
        _mock_embeddings(monkeypatch)

        first = {}
        _mock_upsert(monkeypatch, first)
        await ingestor.ingest_text("Some original content here.", title="T", ctx=TEST_CTX, source="text")

        second = {}
        _mock_upsert(monkeypatch, second)
        await ingestor.ingest_text("Completely different content.", title="T", ctx=TEST_CTX, source="text")

        assert {p.id for p in first["points"]}.isdisjoint({p.id for p in second["points"]})

    async def test_different_tenants_never_collide_onto_the_same_point_id(self, monkeypatch):
        """tenant is folded into the id specifically so two tenants
        uploading byte-identical content never silently share (and
        overwrite) one another's point — see _content_point_id's own
        docstring."""
        _mock_embeddings(monkeypatch)
        other_ctx = {"tenant": "other-co", "principal": "p2", "claims": {}}
        text = "Identical content, different tenants."

        first = {}
        _mock_upsert(monkeypatch, first)
        await ingestor.ingest_text(text, title="T", ctx=TEST_CTX, source="text")

        second = {}
        _mock_upsert(monkeypatch, second)
        await ingestor.ingest_text(text, title="T", ctx=other_ctx, source="text")

        assert {p.id for p in first["points"]}.isdisjoint({p.id for p in second["points"]})

    async def test_different_source_produces_different_point_ids_for_the_same_text(self, monkeypatch):
        _mock_embeddings(monkeypatch)
        text = "Same text, different place it came from."

        first = {}
        _mock_upsert(monkeypatch, first)
        await ingestor.ingest_text(text, title="T", ctx=TEST_CTX, source="upload:a.pdf")

        second = {}
        _mock_upsert(monkeypatch, second)
        await ingestor.ingest_text(text, title="T", ctx=TEST_CTX, source="upload:b.pdf")

        assert {p.id for p in first["points"]}.isdisjoint({p.id for p in second["points"]})

    async def test_sparse_embedding_failure_degrades_to_dense_only(self, monkeypatch):
        async def fake_embed_texts(texts):
            return [[0.1] for _ in texts]

        monkeypatch.setattr(embeddings, "embed_documents", fake_embed_texts)

        def failing_sparse_batch(texts):
            raise RuntimeError("model not loaded")

        monkeypatch.setattr(embeddings, "embed_sparse_batch", failing_sparse_batch)
        captured = {}
        _mock_upsert(monkeypatch, captured)

        await ingestor.ingest_text("some content to ingest", title="T", ctx=TEST_CTX)

        assert "sparse" not in captured["points"][0].vector
        assert captured["points"][0].vector["dense"] == [0.1]

    async def test_embeds_all_chunks_in_one_batched_call_each(self, monkeypatch):
        """The actual point of this change — proves the speedup, not just
        the shape: however many chunks a document produces, embed_texts/
        embed_sparse_batch are each called exactly ONCE, not once per
        chunk (see embed_texts's own docstring for why that matters)."""
        dense_calls = []
        sparse_calls = []

        async def fake_embed_texts(texts):
            dense_calls.append(texts)
            return [[0.1] for _ in texts]

        monkeypatch.setattr(embeddings, "embed_documents", fake_embed_texts)
        monkeypatch.setattr(
            embeddings,
            "embed_sparse_batch",
            lambda texts: sparse_calls.append(texts) or [([1], [0.5]) for _ in texts],
        )

        async def fake_upsert(points):
            pass

        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        text = "Paragraph one about checkpointers.\n\nParagraph two about Qdrant."
        count = await ingestor.ingest_text(text, title="T", ctx=TEST_CTX)

        assert len(dense_calls) == 1
        assert len(sparse_calls) == 1
        assert len(dense_calls[0]) == count
        assert len(sparse_calls[0]) == count


class TestIngestFile:
    async def test_refuses_unsupported_file_types(self, tmp_path):
        p = tmp_path / "doc.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor.ingest_file(str(p), ctx=TEST_CTX)

    async def test_ingests_a_txt_file(self, tmp_path, monkeypatch):
        _mock_embeddings(monkeypatch)
        captured = {}
        _mock_upsert(monkeypatch, captured)

        p = tmp_path / "notes.txt"
        p.write_text("Some notes about the project.", encoding="utf-8")
        count = await ingestor.ingest_file(str(p), ctx=TEST_CTX)

        assert count >= 1
        assert captured["points"][0].payload["source"] == "file:notes.txt"
        assert captured["points"][0].payload["title"] == "notes"

    async def test_ingests_a_md_file(self, tmp_path, monkeypatch):
        _mock_embeddings(monkeypatch)

        async def fake_upsert(points):
            pass

        monkeypatch.setattr(qdrant_store, "upsert", fake_upsert)

        p = tmp_path / "readme.md"
        p.write_text("# Title\n\nSome markdown content.", encoding="utf-8")
        count = await ingestor.ingest_file(str(p), ctx=TEST_CTX)

        assert count >= 1

    async def test_reads_the_file_off_the_event_loop(self, tmp_path, monkeypatch):
        """Read inline, an upload-sized file stalls every other coroutine in the process for the length of the read."""
        _mock_embeddings(monkeypatch)
        _mock_upsert(monkeypatch, {})
        read_on: list[int] = []
        real_read_text = Path.read_text

        def recording_read_text(self, *args, **kwargs):
            read_on.append(threading.get_ident())
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", recording_read_text)
        p = tmp_path / "notes.txt"
        p.write_text("Some notes about the project.", encoding="utf-8")

        await ingestor.ingest_file(str(p), ctx=TEST_CTX)

        assert read_on, "the file was never read through Path.read_text"
        assert threading.get_ident() not in read_on  # this test runs on the event loop's thread


class TestAssertSafeUrl:
    """`ingestor._assert_safe_url` is the async wrapper around `url_safety.assert_safe_url_async` that turns a
    refusal into `IngestRefusedError` + the refusal metric.
    """

    async def test_rejects_non_https_scheme(self):
        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor._assert_safe_url("http://example.com")

    async def test_rejects_a_url_resolving_to_a_private_address(self, monkeypatch):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(2, 1, 6, "", ("10.0.0.5", 0))],
        )
        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor._assert_safe_url("https://internal.example.com")

    async def test_rejects_a_url_resolving_to_loopback(self, monkeypatch):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(2, 1, 6, "", ("127.0.0.1", 0))],
        )
        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor._assert_safe_url("https://localhost.example.com")

    async def test_rejects_if_any_resolved_address_is_private_even_if_others_are_public(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [
                (2, 1, 6, "", ("93.184.216.34", 0)),  # public
                (2, 1, 6, "", ("192.168.1.1", 0)),  # private
            ],
        )
        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor._assert_safe_url("https://mixed.example.com")

    async def test_allows_a_url_resolving_only_to_public_addresses(self, monkeypatch):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )
        await ingestor._assert_safe_url("https://example.com")  # must not raise

    async def test_rejects_unresolvable_host(self, monkeypatch):
        def raise_gaierror(host, port, *args, **kwargs):
            raise socket.gaierror("nodename nor servname provided")

        monkeypatch.setattr(socket, "getaddrinfo", raise_gaierror)
        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor._assert_safe_url("https://nonexistent.invalid")

    async def test_a_refusal_is_counted_as_ssrf_blocked(self, monkeypatch):
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, port, *args, **kwargs: [(2, 1, 6, "", ("100.100.100.200", 0))],
        )
        before = metric_value(ingestor.metrics.agent_ingest_refused_total, reason="ssrf_blocked")

        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor._assert_safe_url("https://metadata.example.com")

        after = metric_value(ingestor.metrics.agent_ingest_refused_total, reason="ssrf_blocked")
        assert after == before + 1


async def _allow_any_url(url):
    return None


class _Fetcher:
    """The `PageFetcher` port, faked: what `ingest_url` does with a page is under test here, not how httpx fetches one (that is
    tests/adapters/outbound/web/test_page_fetch.py)."""

    def __init__(self, page=None, *, raises=None):
        self.page, self.raises, self.calls = page, raises, []

    async def fetch(self, url, *, max_bytes):
        self.calls.append((url, max_bytes))
        if self.raises:
            raise self.raises
        return self.page


class TestIngestUrl:
    async def test_refuses_before_any_fetch_when_url_is_unsafe(self, monkeypatch):
        async def always_unsafe(url):
            raise ingestor.IngestRefusedError("blocked")

        monkeypatch.setattr(ingestor, "_assert_safe_url", always_unsafe)
        with pytest.raises(ingestor.IngestRefusedError):
            await ingestor.ingest_url("https://blocked.example.com", ctx=TEST_CTX)

    async def test_strips_html_before_ingesting(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", _allow_any_url)
        html = "<html><body><script>evil()</script><p>Real content here.</p></body></html>"
        captured = {}

        async def fake_ingest_text(text, title, ctx, source="text", topic=None, *, ingest_id=None):
            captured["text"] = text
            captured["title"] = title
            captured["source"] = source
            captured["ingest_id"] = ingest_id
            return 1

        monkeypatch.setattr(ingestor, "ingest_text", fake_ingest_text)

        with page_fetcher.overriding(_Fetcher(FetchedPage(200, "text/html", html, len(html)))):
            await ingestor.ingest_url("https://example.com/page", ctx=TEST_CTX)

        assert "Real content here." in captured["text"]
        assert "evil()" not in captured["text"]
        assert captured["source"] == "url:https://example.com/page"
        assert captured["ingest_id"], "a page is one document: fetching it again replaces the earlier copy (R18)"

    async def test_refuses_when_response_exceeds_size_limit(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", _allow_any_url)

        with page_fetcher.overriding(_Fetcher(FetchedPage(200, "", "", ingestor._MAX_URL_BYTES + 1))), pytest.raises(ingestor.IngestRefusedError):
            await ingestor.ingest_url("https://example.com/huge", ctx=TEST_CTX)

    async def test_refuses_on_http_error_status(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", _allow_any_url)

        with page_fetcher.overriding(_Fetcher(FetchedPage(404, "", "", 0))), pytest.raises(ingestor.IngestRefusedError):
            await ingestor.ingest_url("https://example.com/missing", ctx=TEST_CTX)

    async def test_refuses_on_transport_error(self, monkeypatch):
        monkeypatch.setattr(ingestor, "_assert_safe_url", _allow_any_url)

        with page_fetcher.overriding(_Fetcher(raises=PageFetchFailedError("refused"))), pytest.raises(ingestor.IngestRefusedError):
            await ingestor.ingest_url("https://example.com/down", ctx=TEST_CTX)

    async def test_asks_the_fetcher_once_with_its_size_limit(self, monkeypatch):
        """The fetcher streams and stops at `max_bytes` (PRODUCTION_REVIEW.md R20); `ingest_url` is what tells it how many. That a redirect is not followed is the
        fetcher's own contract (test_page_fetch.py), not something to re-test through a fake of it."""
        monkeypatch.setattr(ingestor, "_assert_safe_url", _allow_any_url)

        async def fake_ingest_text(*a, **kw):
            return 0

        monkeypatch.setattr(ingestor, "ingest_text", fake_ingest_text)
        fetcher = _Fetcher(FetchedPage(200, "text/plain", "x", 1))

        with page_fetcher.overriding(fetcher):
            await ingestor.ingest_url("https://example.com/page", ctx=TEST_CTX)

        assert fetcher.calls == [("https://example.com/page", ingestor._MAX_URL_BYTES)]


class _FakeEmbedder:
    async def embed_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]

    def embed_sparse_batch(self, texts):
        return [([1], [0.5]) for _ in texts]


class _RecordingWriter:
    def __init__(self):
        self.points = []

    async def upsert(self, points):
        self.points.extend(points)


async def test_the_pipeline_needs_only_its_ports_and_stamps_every_chunk_with_the_callers_tenant():
    """No adapter module is involved: with a fake bound to each port, the pipeline chunks, embeds and writes, and every point carries the tenant."""
    from app.ports.outbound.retrieval import DocumentPoint, document_writer, embedder

    writer = _RecordingWriter()
    with embedder.overriding(_FakeEmbedder()), document_writer.overriding(writer):
        count = await ingestor.ingest_text("A sentence. " * 40, title="T", ctx=TEST_CTX, source="text")

    assert count == len(writer.points) > 0
    assert all(isinstance(p, DocumentPoint) for p in writer.points)
    assert {p.payload["tenant"] for p in writer.points} == {TEST_CTX["tenant"]}
    assert len({p.point_id for p in writer.points}) == len(writer.points)
