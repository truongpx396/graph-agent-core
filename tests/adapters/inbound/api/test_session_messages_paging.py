"""`GET /chat/sessions/{thread_id}/messages?limit=&before=` through the real app (PRODUCTION_REVIEW.md R27), so the bounds are the ones a client meets.

`TestClient(app)` is built WITHOUT `with`, so no lifespan runs (the same arrangement as tests/adapters/inbound/api/test_deps_auth_modes.py).
"""
from fastapi.testclient import TestClient

from app.adapters.inbound.api import main as api
from app.adapters.inbound.api.routers import chat as chat_router
from app.agent.runtime import sessions

HEADERS = {"X-Tenant-Id": "ecorp", "X-Principal-Id": "p1"}


def _client(monkeypatch, seen):
    async def owned(ctx, thread_id, domain):
        return True

    async def fake_get_session_messages(thread_id, *, limit=None, before=None):
        seen.append((limit, before))
        return [{"index": 7, "role": "user", "text": "hi"}]

    monkeypatch.setattr(sessions, "session_belongs_to", owned)
    monkeypatch.setattr(chat_router, "get_session_messages", fake_get_session_messages)
    return TestClient(api.app)


def test_a_page_is_asked_for_with_limit_and_before_and_each_message_says_where_it_sits(monkeypatch):
    seen = []

    response = _client(monkeypatch, seen).get("/chat/sessions/t1/messages?limit=50&before=57", headers=HEADERS)

    assert response.status_code == 200
    assert response.json() == [{"index": 7, "role": "user", "text": "hi"}]
    assert seen == [(50, 57)]


def test_no_parameters_still_means_the_whole_transcript(monkeypatch):
    seen = []

    assert _client(monkeypatch, seen).get("/chat/sessions/t1/messages", headers=HEADERS).status_code == 200
    assert seen == [(None, None)]


def test_a_bound_nobody_could_use_is_a_422_and_never_reaches_the_transcript(monkeypatch):
    seen = []
    client = _client(monkeypatch, seen)

    statuses = [client.get(f"/chat/sessions/t1/messages?{query}", headers=HEADERS).status_code for query in ("limit=0", "limit=501", "before=-1", "limit=abc")]

    assert statuses == [422, 422, 422, 422]
    assert seen == []
