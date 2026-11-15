"""`embed_query` remembers what it embedded and shares a request between concurrent callers (PRODUCTION_REVIEW.md R10).

A turn embedded the person's question in `check_semantic_cache`, again in each of the two searches of `gather_context` (documents and memories, which run at
the same time) and again in `write_semantic_cache`: up to four identical calls to the embedding model where one would do.
"""
import asyncio
from types import SimpleNamespace

import pytest

from app.adapters.outbound.embeddings import embeddings as emb


class _Model:
    """Stands in for the OpenAIEmbeddings client: counts calls, and lets a test hold one open."""

    def __init__(self):
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.fail_first = False

    async def aembed_query(self, text):
        self.calls.append(text)
        if self.gate is not None:
            await self.gate.wait()
        if self.fail_first and len(self.calls) == 1:
            raise ConnectionError("the embedding model went away")
        return [float(len(text)), 1.0, 2.0]


@pytest.fixture
def model(monkeypatch):
    fake = _Model()
    monkeypatch.setattr(emb, "embeddings", fake)
    monkeypatch.setattr(emb, "EMBED_QUERY_PREFIX", "")
    return fake


async def test_the_same_question_is_embedded_once_within_a_turn(model):
    """The cache read, both searches (run at the same time) and the cache write: four calls, one embedding."""
    first = await emb.embed_query("what is the refund policy?")
    both_searches = await asyncio.gather(emb.embed_query("what is the refund policy?"), emb.embed_query("what is the refund policy?"))
    write = await emb.embed_query("what is the refund policy?")

    assert model.calls == ["what is the refund policy?"]
    assert first == both_searches[0] == both_searches[1] == write


async def test_two_concurrent_identical_queries_share_one_call(model):
    model.gate = asyncio.Event()
    calls = [asyncio.create_task(emb.embed_query("same")) for _ in range(5)]
    await asyncio.sleep(0.02)  # all five are waiting on the one request

    assert model.calls == ["same"]
    model.gate.set()

    assert len({tuple(v) for v in await asyncio.gather(*calls)}) == 1
    assert model.calls == ["same"]


async def test_a_different_text_is_a_different_request(model):
    await emb.embed_query("one")
    await emb.embed_query("two")

    assert model.calls == ["one", "two"]


async def test_the_prefix_is_part_of_the_key(model, monkeypatch):
    """A different prefix is a different embedding of the same words."""
    await emb.embed_query("hello")
    monkeypatch.setattr(emb, "EMBED_QUERY_PREFIX", "search_query: ")
    await emb.embed_query("hello")

    assert model.calls == ["hello", "search_query: hello"]


async def test_a_caller_cannot_change_what_the_next_one_gets(model):
    first = await emb.embed_query("question")
    first[0] = -1.0

    assert (await emb.embed_query("question"))[0] != -1.0


async def test_an_entry_expires_after_its_lifetime(model, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(emb, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    await emb.embed_query("question")

    clock[0] += emb._QUERY_CACHE_TTL_SECONDS - 1
    await emb.embed_query("question")
    assert len(model.calls) == 1

    clock[0] += 2
    await emb.embed_query("question")
    assert len(model.calls) == 2


async def test_the_cache_is_bounded_and_drops_the_least_recently_used(model, monkeypatch):
    monkeypatch.setattr(emb, "_QUERY_CACHE_MAX_ENTRIES", 3)
    for text in ("a", "b", "c"):
        await emb.embed_query(text)
    await emb.embed_query("a")  # a is now the most recently used
    await emb.embed_query("d")  # evicts b, the least

    model.calls.clear()
    await emb.embed_query("a")
    await emb.embed_query("c")
    assert model.calls == []
    await emb.embed_query("b")
    assert model.calls == ["b"]


async def test_a_failure_is_not_cached_and_reaches_only_its_own_caller(model):
    model.fail_first = True

    with pytest.raises(ConnectionError):
        await emb.embed_query("question")

    assert await emb.embed_query("question") == [8.0, 1.0, 2.0]
    assert len(model.calls) == 2


async def test_a_waiter_whose_leader_failed_asks_for_itself_instead_of_inheriting_the_failure(model):
    model.gate = asyncio.Event()
    model.fail_first = True
    leader = asyncio.create_task(emb.embed_query("question"))
    await asyncio.sleep(0.02)
    waiter = asyncio.create_task(emb.embed_query("question"))
    await asyncio.sleep(0.02)

    model.gate.set()

    with pytest.raises(ConnectionError):
        await leader
    assert await waiter == [8.0, 1.0, 2.0], "one caller's failure is not another's"


async def test_a_cancelled_leader_does_not_cancel_its_waiter(model):
    model.gate = asyncio.Event()
    leader = asyncio.create_task(emb.embed_query("question"))
    await asyncio.sleep(0.02)
    waiter = asyncio.create_task(emb.embed_query("question"))
    await asyncio.sleep(0.02)

    leader.cancel()
    await asyncio.sleep(0.02)
    model.gate.set()

    assert await waiter == [8.0, 1.0, 2.0]


async def test_a_request_on_another_event_loop_never_waits_on_a_future_from_this_one(model):
    """A future belongs to its loop: the tests and the one-shot scripts run more than one loop per process."""
    model.gate = asyncio.Event()
    here = asyncio.create_task(emb.embed_query("question"))  # in flight on THIS loop, held open
    await asyncio.sleep(0.02)

    async def elsewhere():
        model.gate = None  # let the other loop's own request through
        return await emb.embed_query("question")

    other = await asyncio.wait_for(asyncio.to_thread(lambda: asyncio.run(elsewhere())), 5)

    assert other == [8.0, 1.0, 2.0]
    here.cancel()


async def test_reset_clears_what_was_remembered(model):
    await emb.embed_query("question")

    emb.reset_embed_query_cache()
    await emb.embed_query("question")

    assert len(model.calls) == 2


async def test_embedding_a_stored_passage_is_never_cached_and_never_shares_the_query_cache(model, monkeypatch):
    """A passage is a different operation (specs/014 FR-015): only questions are remembered."""
    calls = []

    class _Docs(_Model):
        async def aembed_documents(self, texts, chunk_size=None):
            calls.append(texts)
            return [[0.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(emb, "embeddings", _Docs())
    await emb.embed_document("a passage")
    await emb.embed_document("a passage")

    assert len(calls) == 2
