"""The queue's timings must stay in order, and a configuration that cannot work is refused at start-up (PRODUCTION_REVIEW.md R05, pattern 99).

Three numbers decide whether the queue is correct and only a comment used to keep them in order: how long a turn may run (`request_timeout_seconds`),
how long a conversation's lock lives, and how long a claimed job may sit before it is presumed abandoned (`agent_worker_reclaim_idle_seconds`).
Raising the turn's limit (a slower model: 300 s is ordinary) made a healthy turn "reclaimed" and run twice while it was still going.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`.
"""
import pytest
from pydantic import ValidationError

from app.core.config import THREAD_LOCK_TTL_SECONDS, Settings

VARIABLES = (
    "REQUEST_TIMEOUT_SECONDS", "AGENT_WORKER_RECLAIM_IDLE_SECONDS", "INGEST_WORKER_RECLAIM_IDLE_SECONDS",
    "UNATTENDED_MAX_DECLINE_ROUNDS", "REQUESTS_STREAM_MAX_AGE_SECONDS",
)


@pytest.fixture(autouse=True)
def _scrubbed_environment(monkeypatch):
    for name in VARIABLES:
        monkeypatch.delenv(name, raising=False)


def _message(**overrides):
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, **overrides)
    return refused.value.errors()[0]["msg"]  # the validator's own text


def test_the_shipped_defaults_are_in_order():
    settings = Settings(_env_file=None)

    longest_turn = settings.request_timeout_seconds * (1 + settings.unattended_max_decline_rounds)
    assert settings.agent_worker_reclaim_idle_seconds >= longest_turn + 30
    assert settings.agent_worker_reclaim_idle_seconds == 300


def test_a_slower_turn_limit_with_the_default_reclaim_idle_is_refused_and_the_message_names_variables_not_values():
    message = _message(request_timeout_seconds=300)

    for name in ("AGENT_WORKER_RECLAIM_IDLE_SECONDS", "REQUEST_TIMEOUT_SECONDS", "UNATTENDED_MAX_DECLINE_ROUNDS"):
        assert name in message
    assert "300" not in message and "1230" not in message, "a settings error is printed at import time to a terminal or a log: names, never values"


def test_raising_the_turn_limit_and_the_reclaim_idle_together_is_accepted():
    settings = Settings(_env_file=None, request_timeout_seconds=300, agent_worker_reclaim_idle_seconds=1300)

    assert settings.agent_worker_reclaim_idle_seconds == 1300


def test_an_unattended_turn_runs_one_stream_per_decline_round_so_more_rounds_need_a_longer_idle():
    """`_decline_every_pause` runs up to 1 + UNATTENDED_MAX_DECLINE_ROUNDS streams, each with its own turn limit."""
    assert "AGENT_WORKER_RECLAIM_IDLE_SECONDS" in _message(unattended_max_decline_rounds=10)


def test_the_idle_must_clear_the_longest_turn_by_a_margin_not_just_equal_it():
    longest_turn = 60 * 4
    assert Settings(_env_file=None, agent_worker_reclaim_idle_seconds=longest_turn + 30).agent_worker_reclaim_idle_seconds == 270
    assert "AGENT_WORKER_RECLAIM_IDLE_SECONDS" in _message(agent_worker_reclaim_idle_seconds=longest_turn + 29)


def test_a_reclaim_idle_shorter_than_the_lock_is_refused_so_a_reclaimed_job_never_finds_a_live_lock():
    """The lock outlives a crashed worker by up to its lifetime; a job reclaimed sooner than that meets THREAD_BUSY."""
    assert "AGENT_WORKER_RECLAIM_IDLE_SECONDS" in _message(request_timeout_seconds=1, unattended_max_decline_rounds=0, agent_worker_reclaim_idle_seconds=THREAD_LOCK_TTL_SECONDS)


def test_a_stream_trimmed_sooner_than_a_job_can_be_reclaimed_is_refused():
    """A trim drops pending entries too: the age has to outlast every reclaim idle."""
    message = _message(requests_stream_max_age_seconds=3600, ingest_worker_reclaim_idle_seconds=7200)

    assert "REQUESTS_STREAM_MAX_AGE_SECONDS" in message and "INGEST_WORKER_RECLAIM_IDLE_SECONDS" in message


def test_the_stream_age_cannot_be_set_below_an_hour():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, requests_stream_max_age_seconds=60)
