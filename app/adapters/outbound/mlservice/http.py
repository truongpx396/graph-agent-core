"""One HTTP client per event loop for the ML service (rerank, prompt guard, label sets), instead of one per call.

Each of the three adapters opened an `httpx.AsyncClient` inside the call, so the moderation check, the routing decision and each rerank of every turn paid a
new TCP connection. They did it because a client's pool is bound to the loop that created it and the calls were thought to arrive from different loops
(tools that ran in worker threads). No tool does that any more: every tool body awaits `arun_with_timeout` on the turn's own loop and nothing in `app/` calls
the synchronous `run_with_timeout` (checked with `grep -rn "[^a]run_with_timeout(" app`). The test suite and the one-shot scripts still run more than one loop
per process, one after another, so the client is keyed by the loop that is running, as `qdrant_store.get_client` is (PRODUCTION_REVIEW.md R03, R10).

Each call keeps its own `timeout=` (a rerank, a prompt guard and a routing decision have different deadlines), passed per request.
"""
import asyncio
import weakref

import httpx

_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=100)
_clients: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, httpx.AsyncClient]" = weakref.WeakKeyDictionary()


def ml_client() -> httpx.AsyncClient:
    """The running loop's client, built on first use. Needs a running loop: every caller is already `async def`."""
    loop = asyncio.get_running_loop()
    client = _clients.get(loop)
    if client is None:
        client = _clients[loop] = httpx.AsyncClient(limits=_LIMITS)
    return client


async def close_ml_client() -> None:
    """Closes and forgets the running loop's client (`app/bootstrap.py::close_adapters`, where a process stops). A no-op if none was opened."""
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client is not None:
        await client.aclose()
