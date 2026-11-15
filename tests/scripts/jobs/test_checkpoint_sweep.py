"""`scripts/jobs/checkpoint_sweep.py` (PRODUCTION_REVIEW.md R11): the operator's entry to the conversation retention sweep.

The sweep's own behaviour is `tests/agent/runtime/test_retention.py`; this is what the job adds: 0 means off, a dry run needs no checkpointer, a real run opens
and always closes it, and what the operator is told."""
from datetime import UTC, datetime, timedelta

import pytest

from app.agent.runtime.retention import SweepResult
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.sessions import session_store
from scripts.jobs import checkpoint_sweep
from tests.fakes.sessions import InMemoryCheckpointing, InMemorySessionStore


@pytest.fixture
def world():
    sessions, saver = InMemorySessionStore(), InMemoryCheckpointing()
    old = datetime.now(UTC) - timedelta(days=100)
    sessions.add("old", last_active_at=old)
    sessions.add("fresh")
    with session_store.overriding(sessions), checkpointing.overriding(saver):
        yield sessions, saver


async def test_a_retention_of_zero_is_off_and_touches_nothing(world):
    sessions, saver = world

    assert await checkpoint_sweep.run_sweep(0) is None

    assert set(sessions.rows) == {"old", "fresh"} and not saver.opened


async def test_a_real_run_opens_the_checkpointer_deletes_and_closes_it(world):
    sessions, saver = world

    result = await checkpoint_sweep.run_sweep(30)

    assert result is not None and result.deleted == 1
    assert set(sessions.rows) == {"fresh"} and saver.deleted == ["old"] and saver.opened and saver.closed


async def test_the_checkpointer_is_closed_even_when_the_sweep_fails(world):
    _, saver = world
    saver.fail_for = {"old"}

    with pytest.raises(ConnectionError):
        await checkpoint_sweep.run_sweep(30)

    assert saver.closed


async def test_a_dry_run_changes_nothing_and_never_opens_the_checkpointer(world):
    sessions, saver = world

    result = await checkpoint_sweep.run_sweep(30, dry_run=True)

    assert result is not None and (result.deleted, result.dry_run) == (1, True)
    assert set(sessions.rows) == {"old", "fresh"} and not saver.opened


def test_the_default_retention_is_the_setting_and_the_setting_defaults_to_off():
    import inspect

    from app.core.config import Settings

    assert Settings.model_fields["checkpoint_retention_days"].default == 0
    assert inspect.signature(checkpoint_sweep.run_sweep).parameters["retention_days"].default == checkpoint_sweep.CHECKPOINT_RETENTION_DAYS


def test_a_negative_retention_is_refused_by_the_settings():
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(checkpoint_retention_days=-1)


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (None, "off"),
        (SweepResult(0, 0, True, False), "Nothing to sweep."),
        (SweepResult(3, 0, True, False), "Deleted 3 conversation(s)."),
        (SweepResult(3, 0, True, True), "Would delete 3 conversation(s)."),
        (SweepResult(3, 1, True, False), "older history is gone"),
        (SweepResult(3, 0, False, False), "run it again"),
    ],
)
def test_what_the_operator_is_told(result, expected):
    assert expected in checkpoint_sweep.describe(result)


def test_main_runs_inside_a_scheduled_job_and_passes_the_dry_run_flag(monkeypatch, capsys):
    seen = {}

    async def run_sweep(*, dry_run=False):
        seen["dry_run"] = dry_run
        return SweepResult(2, 0, True, dry_run)

    entered = []

    class _Job:
        def __init__(self, name):
            entered.append(name)

        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(checkpoint_sweep, "run_sweep", run_sweep)
    monkeypatch.setattr(checkpoint_sweep, "scheduled_job", _Job)

    checkpoint_sweep.main(["--dry-run"])

    assert seen == {"dry_run": True} and entered == ["agent-core-checkpoint-sweep"]
    assert "Would delete 2 conversation(s)." in capsys.readouterr().out
