"""Proof of life for the queue workers: each loop touches a file as it passes, and a health check fails when one has not been touched lately.

The workers bind no port, so the image's HTTP health check could not be used on them and it was disabled: a worker whose event loop was stuck (a
blocking call, a deadlock) stayed "up" and nothing restarted it. `WorkerUnreachable` only fires when a whole domain has no worker. Now the agent and
ingest workers each touch `worker-heartbeat-<queue>-<loop>` under `/tmp` (a tmpfs the read-only production image can write) from the read loop and from
the reclaim loop, and `python -m app.platform.worker_heartbeat` is the container's health check: it exits 1 when any heartbeat file is older than the
limit, or when none exists. One file per loop, so a read loop that is stuck is not hidden by a reclaim loop that is fine.

The gauge `agent_worker_last_loop_timestamp_seconds{queue, loop}` carries the same fact to Prometheus (alert `AgentWorkerLoopStalled`).
Compose does not restart an unhealthy container by itself: what does is in deploy/README.md. PRODUCTION_REVIEW.md R22.

This module is also the health check's entry point, so it imports only the standard library at the top: the probe runs every 30 s in every worker.
"""
import contextlib
import os
import sys
import time
from pathlib import Path

_PREFIX = "worker-heartbeat-"
# A reclaim loop wakes every WORKER_RECLAIM_INTERVAL_SECONDS (60) and a pass takes a moment, so the limit is the larger of two minutes and twice that.
_DEFAULT_MAX_AGE_SECONDS = 120.0


def _directory() -> Path:
    return Path(os.environ.get("WORKER_HEARTBEAT_DIR", "/tmp"))


def beat(queue: str, loop: str) -> None:
    """Records that `loop` of `queue`'s worker has just passed. Never raises: proof of life must not be what takes a worker down."""
    with contextlib.suppress(Exception):  # proof of life is never what takes a worker down
        (_directory() / f"{_PREFIX}{queue}-{loop}").touch()
        from app.core import (
            metrics,  # here and not at the top: the health check below runs this module's main and must not load the metrics SDK
        )

        metrics.agent_worker_last_loop_timestamp_seconds.labels(queue=queue, loop=loop).set(time.time())


def max_age_seconds() -> float:
    interval = float(os.environ.get("WORKER_RECLAIM_INTERVAL_SECONDS") or 0)
    return max(_DEFAULT_MAX_AGE_SECONDS, 2 * interval, float(os.environ.get("WORKER_HEARTBEAT_MAX_AGE_SECONDS") or 0))


def problems(now: float | None = None) -> list[str]:
    """What is wrong, one line each; empty means healthy."""
    now = time.time() if now is None else now
    limit = max_age_seconds()
    files = sorted(_directory().glob(f"{_PREFIX}*"))
    if not files:
        return ["no heartbeat has been written: the worker's loops have not started"]
    found = []
    for path in files:
        age = now - path.stat().st_mtime
        if age > limit:
            found.append(f"{path.name[len(_PREFIX):]} last passed {age:.0f}s ago (limit {limit:.0f}s)")
    return found


def main() -> int:
    found = problems()
    for line in found:
        print(line, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
