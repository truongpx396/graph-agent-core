"""`scripts/jobs/answer_sample.py` (PRODUCTION_REVIEW.md R28): the operator's entry to the answer sample. The sample is `tests/agent/runtime/test_answer_sample.py`'s."""
from datetime import UTC, date, datetime, timedelta

import pytest

from app.agent.runtime.answer_sample import SampleResult
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.sessions import session_store
from scripts.jobs import answer_sample
from tests.fakes.sessions import InMemoryCheckpointing, InMemorySessionStore

DAY = date(2026, 11, 14)


@pytest.fixture
def world():
    sessions, saver = InMemorySessionStore(), InMemoryCheckpointing()
    with session_store.overriding(sessions), checkpointing.overriding(saver):
        yield sessions, saver


async def test_the_job_opens_the_checkpointer_samples_and_closes_it(world):
    _, saver = world

    result = await answer_sample.run_sample(DAY, size=5, tenants=())

    assert result.sampled == 0 and saver.opened and saver.closed


async def test_the_checkpointer_is_closed_even_when_the_sample_fails(world, monkeypatch):
    _, saver = world

    async def boom(**kwargs):
        raise RuntimeError("the sample failed")

    monkeypatch.setattr(answer_sample, "sample_answers", boom)

    with pytest.raises(RuntimeError):
        await answer_sample.run_sample(DAY)

    assert saver.closed


def test_the_default_day_is_yesterday_in_utc():
    assert answer_sample.yesterday() == (datetime.now(UTC) - timedelta(days=1)).date()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (SampleResult(), "nothing to sample"),
        (SampleResult(sampled=10, judged=6, no_citations=3, unreadable=1, findings={"ungrounded_marker": 1, "uncited": 2, "misattributed": 0}), "6 judged (1 ungrounded marker, 2 uncited, 0 misattributed)"),
        (SampleResult(sampled=10, judged=6, no_citations=3, unreadable=1), "3 without citations, 1 unreadable"),
        (SampleResult(sampled=1), "a trend, not a verdict"),
    ],
)
def test_what_the_operator_is_told_is_counts_only(result, expected):
    assert expected in answer_sample.describe(result, DAY)


def test_main_samples_the_given_day_inside_a_scheduled_job_and_prints_the_counts(monkeypatch, capsys):
    seen = {}

    async def run_sample(day):
        seen["day"] = day
        return SampleResult(sampled=2, judged=2)

    entered = []

    class _Job:
        def __init__(self, name):
            entered.append(name)

        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(answer_sample, "run_sample", run_sample)
    monkeypatch.setattr(answer_sample, "scheduled_job", _Job)

    answer_sample.main(["--date", "2026-11-14"])

    assert seen == {"day": DAY} and entered == ["agent-core-answer-sample"]
    assert "Sampled 2 conversation(s)" in capsys.readouterr().out


def test_a_malformed_date_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as caught:
        answer_sample.main(["--date", "yesterday"])

    assert caught.value.code == 2
