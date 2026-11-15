"""The chat client's HTTP bounds (app/core/config.py, PRODUCTION_REVIEW.md R06): inert until an operator lowers them, and a value that
could not do anything is refused at start-up.

`_env_file=None` and a scrubbed environment: `Settings()` otherwise reads the developer's own `.env`.
"""
import pytest
from pydantic import ValidationError

from app.core.config import Settings


@pytest.fixture(autouse=True)
def _scrubbed_environment(monkeypatch):
    for name in ("LLM_READ_TIMEOUT_SECONDS", "LLM_SDK_MAX_RETRIES", "REQUEST_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


def test_by_default_a_call_may_stay_silent_as_long_as_a_turn_may_run_and_the_sdk_does_not_retry():
    settings = Settings(_env_file=None)

    assert settings.llm_read_timeout_seconds is None
    assert settings.effective_llm_read_timeout_seconds == float(settings.request_timeout_seconds)
    assert settings.llm_sdk_max_retries == 0


def test_a_slower_turn_limit_carries_the_default_read_timeout_with_it():
    assert Settings(_env_file=None, request_timeout_seconds=420).effective_llm_read_timeout_seconds == 420.0


def test_a_lower_read_timeout_is_used_as_given():
    assert Settings(_env_file=None, llm_read_timeout_seconds=30).effective_llm_read_timeout_seconds == 30.0


def test_a_read_timeout_longer_than_the_turn_is_refused_and_the_message_names_variables_not_values():
    with pytest.raises(ValidationError) as refused:
        Settings(_env_file=None, request_timeout_seconds=60, llm_read_timeout_seconds=90)

    message = refused.value.errors()[0]["msg"]  # the validator's own text; str(refused.value) adds pydantic's input_value
    assert "LLM_READ_TIMEOUT_SECONDS" in message and "REQUEST_TIMEOUT_SECONDS" in message
    assert "90" not in message and "60" not in message


def test_a_blank_line_in_an_env_file_means_unset(monkeypatch):
    """`LLM_READ_TIMEOUT_SECONDS=` and compose's `${LLM_READ_TIMEOUT_SECONDS:-}` both hand the app an empty string."""
    monkeypatch.setenv("LLM_READ_TIMEOUT_SECONDS", "")

    assert Settings(_env_file=None).llm_read_timeout_seconds is None


@pytest.mark.parametrize("value", [0, -1])
def test_a_read_timeout_that_could_never_succeed_is_refused(value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_read_timeout_seconds=value)


def test_a_negative_retry_count_is_refused():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, llm_sdk_max_retries=-1)
