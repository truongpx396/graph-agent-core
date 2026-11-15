"""A dropped connection no longer loses the rest of an answer (PRODUCTION_REVIEW.md R13).

The turn runs on a worker and its events sit in a Redis stream for five minutes, but the client was never told the request id and there was no route to read
from a position. Now each frame carries its position (`id:`), the response names the job (`X-Request-Id`), a silent stream gets `: keepalive` comments, and
`GET /chat/stream/{request_id}` reads on from `Last-Event-ID`, for the conversation's owner only."""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI

from app.adapters.inbound.api import deps
from app.adapters.inbound.api.routers import chat as chat_router
from app.ports.outbound.jobs import turn_queue
from app.ports.outbound.sessions import session_store
from tests.conftest import TEST_CTX
from tests.fakes.sessions import InMemorySessionStore

EVENTS = [
    ("1731672000001-0", {"type": "token", "content": "a"}),
    ("1731672000002-0", {"type": "token", "content": "b"}),
    ("1731672000003-0", {"type": "done"}),
]


def _after(entry_id: str, after: str) -> bool:
    def key(value: str) -> tuple[int, int]:
        head, _, tail = value.partition("-")
        return int(head), int(tail or 0)

    return key(entry_id) > key(after)


class _Queue:
    """Scripted: yields EVENTS after `after`, `delay` seconds apart, and remembers how it was asked."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.owners: dict[str, str] = {}
        self.calls: list[tuple[str, str, float | None]] = []
        self.closed = False

    async def read_results_from(self, request_id, *, after="0", first_event_deadline_seconds=None):
        self.calls.append((request_id, after, first_event_deadline_seconds))
        try:
            for entry_id, event in EVENTS:
                if _after(entry_id, after):
                    await asyncio.sleep(self.delay)
                    yield entry_id, event
        finally:
            self.closed = True

    async def thread_of_request(self, request_id):
        return self.owners.get(request_id)


@pytest.fixture
def world():
    queue = _Queue()
    store = InMemorySessionStore()
    store.add("mine", tenant=TEST_CTX["tenant"], principal=TEST_CTX["principal"])
    store.add("theirs", tenant=TEST_CTX["tenant"], principal="someone-else")
    queue.owners = {"r-mine": "mine", "r-theirs": "theirs"}
    with turn_queue.overriding(queue), session_store.overriding(store):
        yield queue


@pytest.fixture
async def client(world):
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[deps.get_ctx] = lambda: TEST_CTX
    app.dependency_overrides[deps.get_domain] = lambda: "ecorp"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http


async def _frames(response) -> list[str]:
    return [frame async for frame in response.body_iterator]


async def test_each_frame_carries_the_entry_id_then_the_data_line(world):
    frames = await _frames(chat_router._queued_sse_response("r1"))

    assert frames == [f"id: {entry_id}\ndata: {json.dumps(event)}\n\n" for entry_id, event in EVENTS]


async def test_the_response_names_the_job_so_a_dropped_client_can_come_back_to_it(world):
    response = chat_router._queued_sse_response("req-42")

    assert response.headers["X-Request-Id"] == "req-42"
    assert response.headers["Content-Type"].startswith("text/event-stream")
    await _frames(response)


async def test_a_stream_that_goes_quiet_gets_keepalive_comments_and_no_event_is_lost_or_repeated(world):
    world.delay = 0.15  # three events, 0.15 s apart, with a keepalive due every 0.03 s

    frames = await _frames(chat_router._queued_sse_response("r1", keepalive_seconds=0.03))

    data_frames = [f for f in frames if f.startswith("id: ")]
    assert data_frames == [f"id: {entry_id}\ndata: {json.dumps(event)}\n\n" for entry_id, event in EVENTS], "every event once, in order"
    keepalives = [f for f in frames if f == ": keepalive\n\n"]
    assert len(keepalives) >= 3 and len(keepalives) + len(data_frames) == len(frames), "nothing but events and keepalives"


async def test_no_keepalive_is_sent_while_events_keep_coming(world):
    frames = await _frames(chat_router._queued_sse_response("r1", keepalive_seconds=5))

    assert ": keepalive\n\n" not in frames


async def test_a_client_that_goes_away_stops_the_read_it_was_waiting_on(world):
    world.delay = 5  # the first event is far away
    body = chat_router._queued_sse_response("r1", keepalive_seconds=0.02).body_iterator
    assert await anext(body) == ": keepalive\n\n"

    await body.aclose()  # what the server does when the client disconnects
    await asyncio.sleep(0.05)

    assert world.closed, "the pending read of the results stream was cancelled, not left running"


async def test_a_resumed_read_sends_only_the_events_after_last_event_id(client, world):
    response = await client.get("/chat/stream/r-mine", headers={"Last-Event-ID": EVENTS[0][0]})

    assert response.status_code == 200
    assert world.calls == [("r-mine", EVENTS[0][0], world.calls[0][2])]
    assert response.text == "".join(f"id: {entry_id}\ndata: {json.dumps(event)}\n\n" for entry_id, event in EVENTS[1:])
    assert response.headers["x-request-id"] == "r-mine"


async def test_without_last_event_id_the_whole_turn_is_read_from_the_start(client, world):
    response = await client.get("/chat/stream/r-mine")

    assert response.status_code == 200 and world.calls[0][1] == "0"
    assert response.text.count("id: ") == len(EVENTS)


@pytest.mark.parametrize("header", ["abc", "1731672000001", "1731672000001-", "-0", "0; drop"])
async def test_a_malformed_last_event_id_is_a_422_not_a_read_from_somewhere_odd(client, world, header):
    response = await client.get("/chat/stream/r-mine", headers={"Last-Event-ID": header})

    assert response.status_code == 422 and world.calls == []


async def test_another_persons_request_id_an_unknown_one_and_an_expired_one_are_the_same_404(client, world):
    theirs = await client.get("/chat/stream/r-theirs")
    unknown = await client.get("/chat/stream/never-published")

    assert theirs.status_code == unknown.status_code == 404
    assert theirs.json() == unknown.json() == {"detail": "stream not found"}, "the two cannot be told apart"
    assert world.calls == [], "nothing was read for a caller who does not own the conversation"


async def test_a_request_id_under_another_domain_is_a_404_too(world):
    app = FastAPI()
    app.include_router(chat_router.router)
    app.dependency_overrides[deps.get_ctx] = lambda: TEST_CTX
    app.dependency_overrides[deps.get_domain] = lambda: "sales"  # the conversation was opened under ecorp
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        response = await http.get("/chat/stream/r-mine")

    assert response.status_code == 404 and world.calls == []


async def test_a_resumed_read_has_no_first_event_deadline_of_its_own_beyond_the_ones_the_queue_applies(client, world):
    await client.get("/chat/stream/r-mine", headers={"Last-Event-ID": EVENTS[0][0]})

    assert world.calls[0][2] == chat_router.CHAT_FIRST_RESPONSE_DEADLINE_SECONDS, "the queue ignores it for a resumed read; the route does not invent another"
