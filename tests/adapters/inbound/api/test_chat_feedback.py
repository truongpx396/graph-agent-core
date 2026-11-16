"""`POST /chat/feedback` (PRODUCTION_REVIEW.md R28): the HTTP shape of rating one answer. The rules are `tests/agent/runtime/test_feedback.py`'s."""
import httpx
import pytest
from fastapi import FastAPI

from app.adapters.inbound.api import deps
from app.adapters.inbound.api.routers import chat as chat_router
from app.ports.outbound.feedback import feedback_store
from app.ports.outbound.sessions import session_store
from tests.conftest import TEST_CTX
from tests.fakes.feedback import InMemoryFeedbackStore
from tests.fakes.sessions import InMemorySessionStore


@pytest.fixture
def stores():
    sessions, store = InMemorySessionStore(), InMemoryFeedbackStore()
    sessions.add("mine", tenant=TEST_CTX["tenant"], principal=TEST_CTX["principal"])
    sessions.add("theirs", tenant=TEST_CTX["tenant"], principal="someone-else")
    with session_store.overriding(sessions), feedback_store.overriding(store):
        yield store


@pytest.fixture
async def client(stores):
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[deps.get_ctx] = lambda: TEST_CTX
    app.dependency_overrides[deps.get_domain] = lambda: "ecorp"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def test_a_rating_is_a_204_with_no_body_and_is_stored(client, stores):
    response = await client.post("/chat/feedback", json={"thread_id": "mine", "rating": "up", "message_index": 1, "comment": "helpful"})

    assert response.status_code == 204 and response.content == b""
    assert len(stores.rows) == 1


async def test_an_answer_can_be_identified_by_the_request_id_instead(client, stores):
    response = await client.post("/chat/feedback", json={"thread_id": "mine", "rating": "down", "request_id": "req-1"})

    assert response.status_code == 204 and {key[3] for key in stores.rows} == {"request:req-1"}


async def test_another_persons_session_and_an_unknown_one_are_the_same_404_and_nothing_is_stored(client, stores):
    theirs = await client.post("/chat/feedback", json={"thread_id": "theirs", "rating": "up", "message_index": 0})
    unknown = await client.post("/chat/feedback", json={"thread_id": "nope", "rating": "up", "message_index": 0})

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json() == {"detail": "session not found"}
    assert stores.rows == {}


@pytest.mark.parametrize(
    "body",
    [
        {"thread_id": "mine", "rating": "up"},                                          # no way to tell which answer
        {"thread_id": "mine", "rating": "meh", "message_index": 1},                      # not up or down
        {"thread_id": "mine", "rating": "up", "message_index": -1},
        {"thread_id": "mine", "rating": "up", "message_index": 1, "comment": "x" * 1001},
        {"thread_id": "", "rating": "up", "message_index": 1},
        {"thread_id": "t" * 201, "rating": "up", "message_index": 1},
        {"thread_id": "mine", "rating": "up", "request_id": ""},
        {"rating": "up", "message_index": 1},
    ],
    ids=["unidentified", "bad rating", "negative index", "long comment", "empty thread", "long thread", "empty request id", "no thread"],
)
async def test_a_malformed_request_is_a_422_and_stores_nothing(client, stores, body):
    response = await client.post("/chat/feedback", json=body)

    assert response.status_code == 422 and stores.rows == {}
