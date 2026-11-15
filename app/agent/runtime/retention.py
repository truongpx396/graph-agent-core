"""Retention for conversations: the sweep behind `scripts/jobs/checkpoint_sweep.py` (PRODUCTION_REVIEW.md R11).

Every step of every turn writes a checkpoint, and each change to `messages` stores the whole list again, so a long conversation costs far more than its
text, and nothing ever removed any of it. This deletes a conversation `CHECKPOINT_RETENTION_DAYS` after its last message: its checkpoints and its row in
the session directory.

## Order

Checkpoints first, the row second. A crash between the two leaves a row with nothing behind it, which the next run lists again and removes (`delete_thread`
is idempotent). The other order would leave checkpoints no row points at, which nothing would ever find again.

## What it does not touch

Billing and spend (the usage events and the wallet), the person's cross-session memories, the copies in the gateway's spend logs and in Langfuse, and
anything inside a conversation that is still within its retention: trimming old checkpoints of a LIVE conversation is not done here (it needs the saver's
schema and its own tests).

## A conversation that wakes up while the sweep runs

The row is deleted only if it is still inactive (`inactive_before`), so a thread that got a message during the run keeps its row and is counted as
`revived`. Its checkpoints were already deleted by then, so such a conversation can lose its older history: the same loss retention would have caused a
moment earlier, for a conversation that had been quiet for the whole retention period. A person who sends on a deleted thread simply starts a new
conversation under the same id.

A conversation paused at an approval is inactive like any other: after the retention period its pending approval is gone with it, which is the safe
direction (an approval that can no longer be given runs nothing).

The sweep spans tenants on purpose (retention is a property of the table). It is an operator's job, never reachable from a request.
"""
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.core import metrics
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.sessions import session_store

logger = logging.getLogger(__name__)

# Small, because each conversation costs a delete of its checkpoints before its row goes, and a conversation that wakes up mid-batch is only noticed
# when its own row is reached: the smaller the page, the shorter that window.
SWEEP_BATCH_SIZE = 100
# 500 batches is 50,000 conversations a run. A first run over years of history stops at this and says so; run it again.
SWEEP_MAX_BATCHES = 500


@dataclass(frozen=True)
class SweepResult:
    deleted: int  # conversations removed (in a dry run, that would be)
    revived: int  # got a message while the sweep ran: their row was kept
    complete: bool  # False when the per-run ceiling stopped it: run it again
    dry_run: bool


async def sweep_inactive_conversations(
    *,
    retention_days: int,
    dry_run: bool = False,
    batch_size: int = SWEEP_BATCH_SIZE,
    max_batches: int = SWEEP_MAX_BATCHES,
    now: datetime | None = None,
) -> SweepResult:
    """Deletes every conversation whose last message is more than `retention_days` old. Raises `ValueError` for fewer than one day (0 means "off" and is the
    caller's to handle, so a mistyped setting cannot delete everything), before anything is touched. A failure of the store stops the run, loudly; the next
    run continues where it stopped, since every step is idempotent. `dry_run` lists and counts and changes nothing, and needs no checkpointer."""
    if retention_days < 1:
        raise ValueError(f"retention_days={retention_days} must be at least 1 (0 is how the setting says off)")
    cutoff = (now or datetime.now(UTC)) - timedelta(days=retention_days)
    deleted = revived = 0
    complete = False
    after: tuple[datetime, str] | None = None
    for _ in range(max_batches):
        rows = await session_store.get().list_inactive(cutoff, batch_size, after)
        for row in rows:
            if dry_run:
                deleted += 1
                continue
            await checkpointing.get().delete_thread(row["thread_id"])
            if await session_store.get().delete(
                row["tenant"], row["principal"], row["thread_id"], row["domain"], inactive_before=cutoff
            ):
                deleted += 1
                metrics.agent_checkpoint_sweep_deleted_total.inc()
            else:
                revived += 1
                logger.warning("checkpoint_sweep_conversation_revived", extra={"inactive_days": retention_days})
        if len(rows) < batch_size:
            complete = True
            break
        after = (rows[-1]["last_active_at"], rows[-1]["thread_id"])
    if not complete:
        logger.warning("checkpoint_sweep_hit_batch_ceiling", extra={"deleted": deleted, "max_batches": max_batches, "batch_size": batch_size})
    return SweepResult(deleted, revived, complete, dry_run)
