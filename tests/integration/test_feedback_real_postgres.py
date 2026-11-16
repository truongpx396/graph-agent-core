"""`PgFeedbackStore` against a REAL Postgres (PRODUCTION_REVIEW.md R28): what a fake connection can only echo.

The statement says "inserted" by `RETURNING (xmax = 0)` and keeps one rating per tenant, person, thread and answer by the primary key; the table refuses a rating that is not
`up` or `down`. Self-skips (never fails) without Docker, like every `integration` test."""
import uuid
from contextlib import asynccontextmanager

import psycopg
import pytest

from app.adapters.outbound.postgres import feedback as pg_feedback
from tests.containers import ensure_postgres
from tests.integration.schema_reapply import reapply

pytestmark = pytest.mark.integration


@pytest.fixture
def appdata_url() -> str:
    return ensure_postgres()["appdata_database_url"]  # a sync fixture: the helper runs its own loop, which an async one could not


@pytest.fixture
async def real_appdata(monkeypatch, appdata_url):
    url = appdata_url
    await reapply(url, "26-chat-feedback.sql")  # the shared container may predate the script; it is idempotent, and `reapply` is the one safe way to run DDL on it

    @asynccontextmanager
    async def get_connection():
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            yield conn

    monkeypatch.setattr(pg_feedback, "get_connection", get_connection)
    return get_connection


async def _record(store, thread, **kwargs):
    base = {"tenant": "acme", "principal": "alice", "domain": "ecorp", "thread_id": thread, "answer_ref": "message:1", "rating": "up", "comment": None, "request_id": None}
    return await store.record(**{**base, **kwargs})


async def _rows(get_connection, thread):
    async with get_connection() as conn:
        cur = await conn.execute("SELECT principal, answer_ref, rating, comment, request_id FROM chat_feedback WHERE thread_id = %s ORDER BY principal, answer_ref", (thread,))
        return await cur.fetchall()


async def test_the_first_rating_is_new_and_a_later_one_of_the_same_answer_replaces_it(real_appdata):
    store, thread = pg_feedback.PgFeedbackStore(), f"fb-{uuid.uuid4().hex}"

    assert await _record(store, thread, rating="up", comment="good", request_id="r1") is True
    assert await _record(store, thread, rating="down", comment="no, wrong", request_id=None) is False, "the same person's rating of the same answer is a replacement"

    assert await _rows(real_appdata, thread) == [("alice", "message:1", "down", "no, wrong", "r1")], "the request id the first rating named is kept"


async def test_another_person_another_answer_and_another_tenant_are_separate_ratings(real_appdata):
    store, thread = pg_feedback.PgFeedbackStore(), f"fb-{uuid.uuid4().hex}"

    assert await _record(store, thread) is True
    assert await _record(store, thread, principal="bob") is True
    assert await _record(store, thread, answer_ref="message:3") is True
    assert await _record(store, thread, tenant="globex") is True

    assert len(await _rows(real_appdata, thread)) == 4


async def test_a_rating_that_is_not_up_or_down_is_refused_by_the_table_itself(real_appdata):
    store, thread = pg_feedback.PgFeedbackStore(), f"fb-{uuid.uuid4().hex}"

    with pytest.raises(psycopg.errors.CheckViolation):
        await _record(store, thread, rating="sideways")

    assert await _rows(real_appdata, thread) == []
