"""`feedback.record_answer_feedback` (`app/agent/runtime/feedback.py`, PRODUCTION_REVIEW.md R28): a person rates one answer, advisorily.

Ownership is the boundary (nothing is written for a conversation the caller does not own, and it reads as not found); the rating is one row per person per answer, replaced
by a later one and counted once; and the comment is bounded and never logged."""
import logging

import pytest

from app.agent.runtime import feedback
from app.core import metrics
from app.ports.outbound.feedback import feedback_store
from app.ports.outbound.sessions import session_store
from tests.conftest import TEST_CTX, metric_value
from tests.fakes.feedback import InMemoryFeedbackStore
from tests.fakes.sessions import InMemorySessionStore

TENANT, PRINCIPAL = TEST_CTX["tenant"], TEST_CTX["principal"]


@pytest.fixture
def world():
    sessions, store = InMemorySessionStore(), InMemoryFeedbackStore()
    sessions.add("mine", tenant=TENANT, principal=PRINCIPAL)
    sessions.add("theirs", tenant=TENANT, principal="someone-else")
    with session_store.overriding(sessions), feedback_store.overriding(store):
        yield store


async def _rate(thread="mine", **kwargs):
    kwargs.setdefault("rating", "up")
    kwargs.setdefault("message_index", 1)
    return await feedback.record_answer_feedback(TEST_CTX, thread, "ecorp", **kwargs)


async def test_a_rating_of_the_callers_own_conversation_is_stored_with_its_tenant_and_person(world):
    assert await _rate(rating="down", comment="Wrong refund window.") is True

    assert world.rows == {(TENANT, PRINCIPAL, "mine", "message:1"): {"domain": "ecorp", "rating": "down", "comment": "Wrong refund window.", "request_id": None}}


async def test_another_persons_conversation_and_an_unknown_one_are_not_found_and_nothing_is_written(world):
    assert await _rate("theirs") is False
    assert await _rate("never-existed") is False
    assert await feedback.record_answer_feedback(None, "mine", "ecorp", rating="up", message_index=1) is False

    assert world.rows == {}


async def test_another_domain_is_not_found_either(world):
    assert await feedback.record_answer_feedback(TEST_CTX, "mine", "sales", rating="up", message_index=1) is False
    assert world.rows == {}


async def test_the_answer_is_identified_by_its_message_index_else_its_request_id(world):
    await _rate(message_index=3)
    await _rate(message_index=None, request_id="req-9")

    assert {key[3] for key in world.rows} == {"message:3", "request:req-9"}


async def test_when_both_are_given_the_message_index_wins_so_rating_the_same_answer_both_ways_is_one_row(world):
    await _rate(message_index=3, request_id="req-9", rating="up")
    await _rate(message_index=3, request_id=None, rating="down")

    assert len(world.rows) == 1
    assert world.rows[(TENANT, PRINCIPAL, "mine", "message:3")]["rating"] == "down"
    assert world.rows[(TENANT, PRINCIPAL, "mine", "message:3")]["request_id"] == "req-9", "the request id the first rating named is kept"


async def test_an_answer_that_is_not_identified_is_an_error_not_a_guess(world):
    with pytest.raises(ValueError, match="message_index or its request_id"):
        await _rate(message_index=None, request_id=None)

    assert world.rows == {}


async def test_rating_the_same_answer_again_replaces_the_rating_and_counts_once(world):
    before = {r: metric_value(metrics.agent_feedback_total, rating=r) for r in ("up", "down")}

    await _rate(rating="up", comment="good")
    await _rate(rating="down", comment="on reflection, no")

    assert len(world.rows) == 1
    (row,) = world.rows.values()
    assert (row["rating"], row["comment"]) == ("down", "on reflection, no")
    assert metric_value(metrics.agent_feedback_total, rating="up") == before["up"] + 1
    assert metric_value(metrics.agent_feedback_total, rating="down") == before["down"], "a changed mind is not a second vote"


async def test_two_different_answers_are_two_ratings(world):
    await _rate(message_index=1)
    await _rate(message_index=3)

    assert len(world.rows) == 2


async def test_a_comment_is_trimmed_bounded_and_an_empty_one_is_none(world):
    await _rate(message_index=1, comment="   spaced   ")
    await _rate(message_index=2, comment="   ")
    await _rate(message_index=3, comment="x" * (feedback.COMMENT_MAX_CHARS + 500))

    comments = {key[3]: row["comment"] for key, row in world.rows.items()}
    assert comments["message:1"] == "spaced" and comments["message:2"] is None
    assert len(comments["message:3"]) == feedback.COMMENT_MAX_CHARS


async def test_the_comment_is_never_logged(world, caplog):
    with caplog.at_level(logging.DEBUG):
        await _rate(comment="my password is hunter2")

    assert "hunter2" not in caplog.text


async def test_a_store_that_is_down_raises_instead_of_pretending_the_rating_was_kept(world):
    world.fail = ConnectionError("appdata went away")

    with pytest.raises(ConnectionError):
        await _rate()
