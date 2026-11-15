"""`DELETE /chat/sessions/{thread_id}` (PRODUCTION_REVIEW.md R11, part 2): the HTTP shape of deleting one's own conversation.

The rules are `tests/agent/runtime/test_delete_session.py`'s; this is the status codes and the `thread_busy` envelope, over HTTP on a minimal app with only this
router (no lifespan, as `test_ingest_delete.py` does)."""
import httpx
import pytest
from fastapi import FastAPI

from app.adapters.inbound.api import deps
from app.adapters.inbound.api.routers import chat as chat_router
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.jobs import turn_queue
from app.ports.outbound.sessions import session_store
from tests.conftest import TEST_CTX
from tests.fakes.sessions import InMemoryCheckpointing, InMemorySessionStore


class _Queue:
    def __init__(self):
        self.busy: set[str] = set()

    async def is_thread_busy(self, thread_id: str) -> bool:
        return thread_id in self.busy


@pytest.fixture
def world():
    store, saver, queue = InMemorySessionStore(), InMemoryCheckpointing(), _Queue()
    saver.opened = True
    store.add("mine", tenant=TEST_CTX["tenant"], principal=TEST_CTX["principal"])
    store.add("theirs", tenant=TEST_CTX["tenant"], principal="someone-else")
    with session_store.overriding(store), checkpointing.overriding(saver), turn_queue.overriding(queue):
        yield store, saver, queue


@pytest.fixture
async def client(world):
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[deps.get_ctx] = lambda: TEST_CTX
    app.dependency_overrides[deps.get_domain] = lambda: "ecorp"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_the_owner_gets_a_204_with_no_body_and_the_conversation_is_gone(client, world):
    store, saver, _ = world

    response = await client.delete("/chat/sessions/mine")

    assert response.status_code == 204 and response.content == b""
    assert "mine" not in store.rows and saver.deleted == ["mine"]


async def test_a_second_delete_is_a_404(client):
    await client.delete("/chat/sessions/mine")

    assert (await client.delete("/chat/sessions/mine")).status_code == 404


async def test_another_persons_session_and_an_unknown_one_are_the_same_404_and_nothing_is_deleted(client, world):
    store, saver, _ = world

    theirs = await client.delete("/chat/sessions/theirs")
    unknown = await client.delete("/chat/sessions/never-existed")

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json() == {"detail": "session not found"}, "the two cannot be told apart"
    assert "theirs" in store.rows and saver.deleted == []


async def test_a_session_with_a_turn_running_is_a_409_with_the_thread_busy_envelope_and_survives(client, world):
    store, saver, queue = world
    queue.busy.add("mine")

    response = await client.delete("/chat/sessions/mine")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "thread_busy" and body["message"] and body["details"] is None
    assert "mine" in store.rows and saver.deleted == []


async def test_once_the_turn_has_finished_the_same_request_succeeds(client, world):
    _, _, queue = world
    queue.busy.add("mine")
    assert (await client.delete("/chat/sessions/mine")).status_code == 409
    queue.busy.clear()

    assert (await client.delete("/chat/sessions/mine")).status_code == 204


def test_the_responses_the_route_documents_are_the_ones_it_answers_with():
    route = next(r for r in chat_router.router.routes if r.path == "/chat/sessions/{thread_id}" and "DELETE" in r.methods)

    assert route.status_code == 204 and set(route.responses) == {404, 409}
