"""Embedding clients: dense (via the LiteLLM proxy) + sparse (local, via
fastembed) + rerank (via a dedicated reranker container) — the three legs
of hybrid retrieval (GRAPH_PATTERNS.md pattern 20).

Dense stays routed through LiteLLM like every other model call (one
gateway, swappable via config, no provider key here). Sparse (BM25) is a
local, deterministic ONNX model — closer in kind to a calculator than a
chat completion — downloading once from Hugging Face and running offline
after that.

Reranking used to be local/fastembed too, but moved to a dedicated
container (`deploy/dev/app.yml`'s `ml-service`; see `ML_SERVICE_URL`'s
config comment for measured numbers) — it was a measured concurrency
bottleneck in-process, worth isolating unlike sparse's cheap lexical scoring.

The fastembed sparse model is lazily constructed (first call pays the
download/load cost, not import time) and cached in a module global — same
reason as `app/agent/runtime/core.py`'s lazy singleton graph: don't pay startup
cost for a path a process might never exercise.
"""
import asyncio
import time
from collections import OrderedDict
from typing import TYPE_CHECKING

from langchain_openai import OpenAIEmbeddings
from pydantic import SecretStr

from app.adapters.outbound.mlservice.http import ml_client
from app.core.config import (
    EMBED_DOCUMENT_PREFIX,
    EMBED_MODEL,
    EMBED_QUERY_PREFIX,
    ML_SERVICE_URL,
    OPENAI_API_BASE,
    OPENAI_API_KEY,
    SPARSE_MODEL,
)
from app.ports.outbound.retrieval import EMBED_BATCH_SIZE, SparseVector

if TYPE_CHECKING:  # fastembed is imported on first use, not at module load
    from fastembed import SparseTextEmbedding

embeddings = OpenAIEmbeddings(
    model=EMBED_MODEL,
    base_url=OPENAI_API_BASE,
    api_key=SecretStr(OPENAI_API_KEY),
    check_embedding_ctx_length=False,  # let the proxy/Ollama handle chunking
)

# Embedding a QUESTION and embedding a stored PASSAGE are two different operations (specs/014 FR-015). Models trained with task
# prefixes (nomic-embed-text: `search_query:` / `search_document:`) place the two in different regions on purpose, so one function
# serving both roles, which is what this module used to do (`embed_text`), cannot be made right by any prefix. The old names are
# DELETED rather than deprecated: a caller that still uses one is an ImportError, and tests/adapters/outbound/embeddings/test_embedding_roles.py fails if
# any module under app/ or scripts/ calls the underlying client around these functions.
#
# Both prefixes default to empty (settings: EMBED_QUERY_PREFIX / EMBED_DOCUMENT_PREFIX), so the text sent is byte-identical to before
# until an operator opts in, with a re-embed (app/adapters/outbound/embeddings/embedding_identity.py).


# A turn embedded the person's question in `check_semantic_cache`, again in each of the two searches of `gather_context` (documents and memories, which
# run at the same time) and again in `write_semantic_cache`: up to four identical calls to the embedding model where one would do (PRODUCTION_REVIEW.md R10).
# So `embed_query` remembers what it embedded, briefly, and two concurrent calls for the same text share one request.
#
# Keyed by the exact text sent (the prefix included), so a different role or prefix is a different entry. The cached vector is only as valid as the
# embedding identity (model, dimension, prefixes, revision): no identity in the key. The app's own settings change only with a restart, and a model
# changed at the gateway clears this cache (embedding_identity.current_identity). Bounded:
# 512 entries and 300 seconds (a vector is about 25 KB at 768 dimensions, so about 12 MB at the limit; a question repeated within a turn needs seconds).
_QUERY_CACHE_MAX_ENTRIES = 512
_QUERY_CACHE_TTL_SECONDS = 300.0
_query_cache: "OrderedDict[str, tuple[float, list[float]]]" = OrderedDict()
# One in-flight request per (loop, text): a future belongs to the loop that made it, and the tests and the one-shot scripts run more than one loop per process.
# A leader that fails resolves its future with None instead of an exception, so a waiter does its own request: one caller's failure, or its cancellation,
# is never the failure of another.
_in_flight: dict[tuple[int, str], "asyncio.Future[list[float] | None]"] = {}


def reset_embed_query_cache() -> None:
    """For tests (it is reset beside `reset_embedding_dim_cache`) and for `current_identity` when it sees the model behind the alias has changed."""
    _query_cache.clear()
    _in_flight.clear()


async def embed_query(text: str) -> list[float]:
    """Embed a search question, or any text compared with other questions (the semantic cache embeds both sides with this). Remembered for a few minutes and
    shared between concurrent callers (see the note above); a caller gets its own copy of the vector."""
    key = EMBED_QUERY_PREFIX + text
    now = time.monotonic()
    cached = _query_cache.get(key)
    if cached is not None:
        if now - cached[0] < _QUERY_CACHE_TTL_SECONDS:
            _query_cache.move_to_end(key)
            return list(cached[1])
        del _query_cache[key]

    flight = (id(asyncio.get_running_loop()), key)
    pending = _in_flight.get(flight)
    if pending is not None:
        shared = await asyncio.shield(pending)
        if shared is not None:
            return list(shared)
        # the first caller failed: fall through and ask for ourselves

    mine: asyncio.Future[list[float] | None] = asyncio.get_running_loop().create_future()
    _in_flight.setdefault(flight, mine)
    leader = _in_flight[flight] is mine
    vector: list[float] | None = None
    try:
        vector = await embeddings.aembed_query(key)
        _query_cache[key] = (time.monotonic(), vector)
        while len(_query_cache) > _QUERY_CACHE_MAX_ENTRIES:
            _query_cache.popitem(last=False)
        return list(vector)
    finally:
        if leader:
            _in_flight.pop(flight, None)
        if not mine.done():
            mine.set_result(vector)


async def embed_document(text: str) -> list[float]:
    """Embed one stored passage, note, memory or skill description."""
    return (await embeddings.aembed_documents([EMBED_DOCUMENT_PREFIX + text], chunk_size=EMBED_BATCH_SIZE))[0]




async def embed_documents(texts: list[str]) -> list[list[float]]:
    """Dense embeddings for MANY stored passages in one batched round trip — the bulk-ingest counterpart to `embed_document`'s
    single call (see `EMBED_BATCH_SIZE`'s comment for why the batch size is capped).

    Batching measured ~4x speedup (46ms/chunk one-at-a-time vs ~11ms/chunk
    at 200/request), dominated by per-HTTP-call overhead, not model
    compute — a real 5700-chunk/9.7MB PDF dropped from ~13min to ~75s."""
    return await embeddings.aembed_documents([EMBED_DOCUMENT_PREFIX + t for t in texts], chunk_size=EMBED_BATCH_SIZE)


_embedding_dim: int | None = None


async def embedding_dim() -> int:
    """The dimension of the configured embedding model, read from a real call (never hardcoded, so it cannot drift from whatever the
    alias points at) and cached for the process. Collections, the answer cache and the identity all size themselves from it."""
    global _embedding_dim
    if _embedding_dim is None:
        _embedding_dim = len(await embed_document("dimension probe"))
    return _embedding_dim


def reset_embedding_dim_cache() -> None:
    """For tests and for `current_identity` when it sees the model behind the alias has changed."""
    global _embedding_dim
    _embedding_dim = None


_sparse_model: "SparseTextEmbedding | None" = None
_RERANK_TIMEOUT_SECONDS = 10


def _get_sparse_model() -> "SparseTextEmbedding":
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding

        _sparse_model = SparseTextEmbedding(model_name=SPARSE_MODEL)
    return _sparse_model


def embed_sparse(text: str) -> tuple[list[int], list[float]]:
    """BM25 sparse vector as (indices, values) — the shape
    `qdrant_store.py` needs to build a `SparseVector`. Raises on failure
    rather than degrading itself — the caller (`hybrid_search`) owns the
    degrade-to-dense-only policy, so failures must surface here, not be
    swallowed twice.
    """
    vec = next(iter(_get_sparse_model().embed([text])))
    return vec.indices.tolist(), vec.values.tolist()


def embed_sparse_batch(texts: list[str]) -> list[tuple[list[int], list[float]]]:
    """Batched sparse/BM25 vectors — same local fastembed model as
    `embed_sparse` (fastembed's `.embed()` already batches ONNX inference
    over a list). Raises on failure; degrade-to-dense-only applies to the
    WHOLE batch at once, not per-text — one bad text degrades the entire
    document's sparse leg (see `ingestor.py::_sparse_vectors_or_none`)."""
    return [
        (vec.indices.tolist(), vec.values.tolist())
        for vec in _get_sparse_model().embed(texts)
    ]


async def rerank(query: str, candidates: list[str]) -> list[float]:
    """Cross-encoder relevance scores, one per candidate, in `candidates` order (higher is more relevant). Raises
    on failure; the caller owns the degrade policy (see `embed_sparse`). A real HTTP call to the `ml-service`
    container (`ML_SERVICE_URL`), so `async def`.

    Through the loop's shared client (`mlservice/http.py`), not one opened per call: a rerank on every turn paid a new TCP connection. The client is
    per event loop because its pool is bound to the loop that created it, and nothing calls this from a worker thread any more (every tool body
    awaits `arun_with_timeout` on the turn's own loop).

    The response is `[{"index", "score", ...}]`, placed back into `candidates` order by `index` rather than
    assumed sorted (TEI's `/rerank` is score-sorted, `ml-service` returns input order; reading `index` depends
    on neither). `raw_scores` is for wire-compatibility with TEI: `ml-service` returns only the raw logit, the
    scale `MIN_RERANK_SCORE` (app/agent/tools/builtin.py) is calibrated to.
    """
    resp = await ml_client().post(
        f"{ML_SERVICE_URL}/rerank",
        json={"query": query, "texts": candidates, "raw_scores": True},
        timeout=_RERANK_TIMEOUT_SECONDS,
    )
    resp.raise_for_status()
    scores = [0.0] * len(candidates)
    for item in resp.json():
        scores[item["index"]] = item["score"]
    return scores


class HybridEmbedder:
    """The `Embedder` adapter (`app/ports/outbound/retrieval.py`): dense vectors through the gateway, sparse ones locally. The module's functions are looked up at call time."""

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return await embed_documents(texts)

    def embed_sparse_batch(self, texts: list[str]) -> list[SparseVector]:
        return embed_sparse_batch(texts)
