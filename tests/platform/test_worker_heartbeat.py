"""The queue workers' proof of life (app/platform/worker_heartbeat.py, PRODUCTION_REVIEW.md R22).

A worker binds no port, so its health check was disabled and a worker whose event loop was stuck stayed "up" with nothing to restart it.
"""
import os
import subprocess
import sys

import pytest

from app.platform import worker_heartbeat
from tests.conftest import metric_value


@pytest.fixture(autouse=True)
def heartbeat_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKER_HEARTBEAT_DIR", str(tmp_path))
    monkeypatch.delenv("WORKER_RECLAIM_INTERVAL_SECONDS", raising=False)
    monkeypatch.delenv("WORKER_HEARTBEAT_MAX_AGE_SECONDS", raising=False)
    return tmp_path


def _age(path, seconds):
    old = path.stat().st_mtime - seconds
    os.utime(path, (old, old))


def test_a_beat_leaves_one_file_per_queue_and_loop(heartbeat_dir):
    worker_heartbeat.beat("agent", "read")
    worker_heartbeat.beat("agent", "reclaim")
    worker_heartbeat.beat("ingest", "read")

    assert sorted(p.name for p in heartbeat_dir.iterdir()) == [
        "worker-heartbeat-agent-read", "worker-heartbeat-agent-reclaim", "worker-heartbeat-ingest-read",
    ]


def test_a_beat_sets_the_gauge_for_that_queue_and_loop():
    worker_heartbeat.beat("agent", "read")

    from app.core import metrics

    assert metric_value(metrics.agent_worker_last_loop_timestamp_seconds, queue="agent", loop="read") > 1_700_000_000


def test_fresh_beats_are_healthy():
    worker_heartbeat.beat("agent", "read")
    worker_heartbeat.beat("agent", "reclaim")

    assert worker_heartbeat.problems() == []
    assert worker_heartbeat.main() == 0


def test_a_loop_that_stopped_passing_is_named_even_when_the_other_one_is_fine(heartbeat_dir, capsys):
    """One file per loop: a stuck read loop must not hide behind a reclaim loop that is still ticking."""
    worker_heartbeat.beat("agent", "read")
    worker_heartbeat.beat("agent", "reclaim")
    _age(heartbeat_dir / "worker-heartbeat-agent-read", 300)

    assert worker_heartbeat.main() == 1
    assert "agent-read last passed" in capsys.readouterr().err
    assert len(worker_heartbeat.problems()) == 1


def test_no_heartbeat_at_all_is_unhealthy():
    """A worker that never reached its loops (compose's start_period covers the time it needs to)."""
    assert worker_heartbeat.main() == 1


def test_the_limit_is_two_minutes_and_follows_a_slower_reclaim_interval(heartbeat_dir, monkeypatch):
    worker_heartbeat.beat("agent", "reclaim")
    _age(heartbeat_dir / "worker-heartbeat-agent-reclaim", 150)
    assert worker_heartbeat.problems() != []

    monkeypatch.setenv("WORKER_RECLAIM_INTERVAL_SECONDS", "120")  # it wakes every 2 minutes: 150 s is on time

    assert worker_heartbeat.problems() == []


def test_a_directory_that_cannot_be_written_never_raises_out_of_a_beat(monkeypatch):
    monkeypatch.setenv("WORKER_HEARTBEAT_DIR", "/nonexistent/readonly")

    worker_heartbeat.beat("agent", "read")  # no exception: proof of life is never what takes a worker down


def _probe(heartbeat_dir):
    return subprocess.run(
        [sys.executable, "-m", "app.platform.worker_heartbeat"],
        env={**os.environ, "WORKER_HEARTBEAT_DIR": str(heartbeat_dir)}, capture_output=True, text=True,
    )


def test_the_probe_compose_runs_exits_one_with_no_heartbeat_and_zero_with_a_fresh_one(heartbeat_dir):
    """`python -m app.platform.worker_heartbeat` is the container's health check."""
    assert (probe := _probe(heartbeat_dir)).returncode == 1 and "no heartbeat" in probe.stderr

    worker_heartbeat.beat("agent", "read")

    assert _probe(heartbeat_dir).returncode == 0


def test_the_probe_loads_no_metrics_sdk_so_one_every_30_seconds_costs_milliseconds():
    imported = subprocess.run(
        [sys.executable, "-c", "import sys, app.platform.worker_heartbeat; print('app.core.metrics' in sys.modules)"], capture_output=True, text=True
    )

    assert imported.stdout.strip() == "False"
