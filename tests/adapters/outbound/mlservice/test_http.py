"""The ML service's shared HTTP client (app/adapters/outbound/mlservice/http.py, PRODUCTION_REVIEW.md R10).

The moderation check, the routing decision and each rerank of every turn opened a new `httpx.AsyncClient` inside the call, so each paid a new TCP
connection. They share one client per event loop now.
"""
import asyncio
import weakref

import httpx
import pytest

from app.adapters.outbound.embeddings import embeddings as emb
from app.adapters.outbound.mlservice import classification, http, prompt_guard
from app.ports.outbound.classification import Choice


class _CountingClient(httpx.AsyncClient):
    """A real AsyncClient over a mock transport that counts how many were ever built and what each request carried."""

    built: list["_CountingClient"] = []
    requests: list[httpx.Request] = []

    def __init__(self, **kwargs):
        def handler(request: httpx.Request) -> httpx.Response:
            _CountingClient.requests.append(request)
            if request.url.path == "/rerank":
                return httpx.Response(200, json=[{"index": 0, "score": 1.5}])
            if request.url.path == "/prompt-guard":
                return httpx.Response(200, json=[{"malicious_score": 0.25}])
            return httpx.Response(503, json={})  # a decision the service cannot make: the router's rules decide

        super().__init__(transport=httpx.MockTransport(handler), **kwargs)
        _CountingClient.built.append(self)


@pytest.fixture(autouse=True)
def counting_clients(monkeypatch):
    _CountingClient.built, _CountingClient.requests = [], []
    monkeypatch.setattr(http.httpx, "AsyncClient", _CountingClient)
    monkeypatch.setattr(http, "_clients", weakref.WeakKeyDictionary())


async def test_the_client_is_built_once_per_loop_not_once_per_call():
    first, second = http.ml_client(), http.ml_client()

    assert first is second and len(_CountingClient.built) == 1


async def test_a_rerank_a_prompt_guard_and_a_routing_decision_share_one_client():
    await emb.rerank("a question", ["a passage"])
    await prompt_guard.MlServicePromptGuard().malicious_score("hello")
    await classification.MlServiceClassifier().choose(Choice("route-topics", "pick one", {"lookup": "a lookup"}), "hello")
    await emb.rerank("another", ["passage"])

    assert len(_CountingClient.built) == 1, "each call opened its own client"
    assert [r.url.path for r in _CountingClient.requests] == ["/rerank", "/prompt-guard", "/v1/decide", "/rerank"]


async def test_each_call_keeps_its_own_deadline():
    """The three have different budgets, so the timeout travels with the request, not the client."""
    await emb.rerank("q", ["p"])
    await prompt_guard.MlServicePromptGuard().malicious_score("hello")

    rerank, guard = _CountingClient.requests
    assert rerank.extensions["timeout"]["read"] == emb._RERANK_TIMEOUT_SECONDS
    assert guard.extensions["timeout"]["read"] == prompt_guard._ML_CHECK_TIMEOUT_SECONDS


async def test_another_event_loop_gets_its_own_client():
    """A pool belongs to the loop that opened it, and the tests and the one-shot scripts run more than one loop per process."""
    here = http.ml_client()

    async def on_a_fresh_loop():
        return http.ml_client()

    elsewhere = await asyncio.to_thread(lambda: asyncio.run(on_a_fresh_loop()))

    assert elsewhere is not here and len(_CountingClient.built) == 2


async def test_the_connection_pool_is_bounded():
    limits = http._LIMITS

    assert (limits.max_keepalive_connections, limits.max_connections) == (20, 100)


async def test_close_ml_client_closes_and_forgets_it_and_with_none_open_is_a_no_op():
    await http.close_ml_client()  # nothing open yet
    client = http.ml_client()

    await http.close_ml_client()

    assert client.is_closed
    assert http.ml_client() is not client, "the next call builds a fresh one, as after a restart"
