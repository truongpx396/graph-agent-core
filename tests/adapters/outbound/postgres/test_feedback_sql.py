"""The statement `PgFeedbackStore.record` runs (PRODUCTION_REVIEW.md R28): an upsert keyed by tenant, person, thread and answer that says whether it inserted."""
from contextlib import asynccontextmanager

import pytest

from app.adapters.outbound.postgres import feedback as pg_feedback


class _Cursor:
    def __init__(self, row):
        self._row = row

    async def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, row):
        self.calls = []
        self._row = row

    async def execute(self, sql, params=()):
        self.calls.append((" ".join(sql.split()), tuple(params)))
        return _Cursor(self._row)


@pytest.mark.parametrize(("row", "expected"), [((True,), True), ((False,), False), (None, False)])
async def test_it_reports_whether_the_row_was_new(monkeypatch, row, expected):
    connection = _Connection(row)

    @asynccontextmanager
    async def get_connection():
        yield connection

    monkeypatch.setattr(pg_feedback, "get_connection", get_connection)

    result = await pg_feedback.PgFeedbackStore().record(
        tenant="acme", principal="alice", domain="ecorp", thread_id="t1", answer_ref="message:1", rating="up", comment="c", request_id="r1"
    )

    assert result is expected
    ((sql, params),) = connection.calls
    assert params == ("acme", "alice", "t1", "message:1", "ecorp", "r1", "up", "c")
    assert "ON CONFLICT (tenant, principal, thread_id, answer_ref) DO UPDATE" in sql and "RETURNING (xmax = 0)" in sql
    assert "COALESCE(EXCLUDED.request_id, chat_feedback.request_id)" in sql, "a rating that names no request id keeps the one an earlier rating gave"
