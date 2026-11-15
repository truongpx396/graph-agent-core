"""Cron-callable retention sweep for conversations: their checkpoints and their rows in the session directory
(app/agent/runtime/retention.py, PRODUCTION_REVIEW.md R11).

Nothing deleted a conversation, so the checkpointer's tables grew with every step of every turn for ever (each change to `messages` stores the whole list
again). This deletes a conversation `CHECKPOINT_RETENTION_DAYS` after its last message. The default is 0, which is OFF: nothing is deleted until an operator
chooses a number. Like the other sweeps, a fixed pipeline with no model involved, deliberately not run automatically: wire it to cron once the retention
policy is decided, e.g. `30 3 * * * cd /path/to/graph-agent-core && python -m scripts.jobs.checkpoint_sweep`.

`--dry-run` lists and counts what it would delete and changes nothing: run it first. A re-run before more conversations age out deletes nothing.
It does not touch spend (the usage events, the wallet), cross-session memories, the gateway's spend logs or Langfuse.
"""
import argparse
import asyncio
import logging
from collections.abc import Sequence

from app.agent.runtime.retention import (
    SWEEP_BATCH_SIZE,
    SWEEP_MAX_BATCHES,
    SweepResult,
    sweep_inactive_conversations,
)
from app.core.config import CHECKPOINT_RETENTION_DAYS
from app.platform.job_runtime import scheduled_job
from app.ports.outbound.checkpointing import checkpointing

logger = logging.getLogger(__name__)


async def run_sweep(retention_days: int = CHECKPOINT_RETENTION_DAYS, *, dry_run: bool = False) -> SweepResult | None:
    """The sweep, or None when retention is off (0). The checkpointer is opened on this loop for a real run and closed after it; a dry run reads only the
    session directory."""
    if retention_days == 0:
        return None
    if dry_run:
        result = await sweep_inactive_conversations(retention_days=retention_days, dry_run=True)
    else:
        await checkpointing.get().open()
        try:
            result = await sweep_inactive_conversations(retention_days=retention_days)
        finally:
            await checkpointing.get().close()
    logger.info(
        "checkpoint_swept",
        extra={"deleted": result.deleted, "revived": result.revived, "retention_days": retention_days, "dry_run": dry_run},
    )
    return result


def describe(result: SweepResult | None) -> str:
    """What the operator sees on stdout."""
    if result is None:
        return "Checkpoint retention is off (CHECKPOINT_RETENTION_DAYS=0): nothing was deleted. Set a number of days to turn it on."
    verb = "Would delete" if result.dry_run else "Deleted"
    lines = [f"{verb} {result.deleted} conversation(s)." if result.deleted else "Nothing to sweep."]
    if result.revived:
        lines.append(f"{result.revived} got a message while the sweep ran: their rows were kept, but their older history is gone.")
    if not result.complete:
        lines.append(f"That is this run's ceiling ({SWEEP_MAX_BATCHES * SWEEP_BATCH_SIZE} conversations), so more may remain: run it again.")
    return " ".join(lines)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Delete conversations older than CHECKPOINT_RETENTION_DAYS (0 = off).")
    parser.add_argument("--dry-run", action="store_true", help="count what would be deleted and change nothing")
    args = parser.parse_args(argv)
    with scheduled_job("agent-core-checkpoint-sweep"):
        print(describe(asyncio.run(run_sweep(dry_run=args.dry_run))))


if __name__ == "__main__":
    main()
