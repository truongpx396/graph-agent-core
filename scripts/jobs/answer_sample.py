"""Cron-callable sample of yesterday's answers, checked for grounding (app/agent/runtime/answer_sample.py, PRODUCTION_REVIEW.md R28).

    30 4 * * * cd /path/to/graph-agent-core && python -m scripts.jobs.answer_sample          # yesterday, UTC
    python -m scripts.jobs.answer_sample --date 2026-11-14                                    # another day

Reads up to `ANSWER_SAMPLE_SIZE` (50) random conversations that were last active that day and runs the grounded-claims checks over each one's final answer and citations,
the same three functions the graph runs on every turn. It calls no model and emits COUNTS (`agent_answer_sample_total{outcome}`, `agent_answer_sample_finding_total{finding}`):
no answer text, no tenant, person or thread id leaves it. ADVISORY: heuristics tuned on small models are a trend to watch, never a verdict, and nothing alerts on them.

It reads answers ACROSS tenants (`ANSWER_SAMPLE_TENANTS` limits it): an operator job, like the retention sweep, never reachable from a request. Leave it unscheduled where
that is not allowed. Not run automatically; wire it to cron once you have decided.
"""
import argparse
import asyncio
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

from app.agent.runtime.answer_sample import FINDINGS, SampleResult, sample_answers
from app.core.config import ANSWER_SAMPLE_SIZE, ANSWER_SAMPLE_TENANTS
from app.platform.job_runtime import scheduled_job
from app.ports.outbound.checkpointing import checkpointing

logger = logging.getLogger(__name__)


def yesterday() -> date:
    return (datetime.now(UTC) - timedelta(days=1)).date()


async def run_sample(day: date, *, size: int = ANSWER_SAMPLE_SIZE, tenants: tuple[str, ...] = ANSWER_SAMPLE_TENANTS) -> SampleResult:
    """The sample, with the checkpointer opened on this loop and always closed."""
    await checkpointing.get().open()
    try:
        result = await sample_answers(day=day, size=size, tenants=tenants)
    finally:
        await checkpointing.get().close()
    logger.info("answer_sampled", extra={"day": day.isoformat(), "sampled": result.sampled, "judged": result.judged, "findings": sum(result.findings.values())})
    return result


def describe(result: SampleResult, day: date) -> str:
    """What the operator sees: counts only."""
    if result.sampled == 0:
        return f"No conversations were last active on {day.isoformat()}: nothing to sample."
    findings = ", ".join(f"{result.findings[name]} {name.replace('_', ' ')}" for name in FINDINGS)
    return (
        f"Sampled {result.sampled} conversation(s) last active on {day.isoformat()}: {result.judged} judged ({findings}), "
        f"{result.no_citations} without citations, {result.unreadable} unreadable. Advisory: a trend, not a verdict."
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Sample yesterday's answers and run the grounded-claims checks (counts only).")
    parser.add_argument("--date", type=date.fromisoformat, default=None, help="the UTC day to sample (YYYY-MM-DD); default yesterday")
    args = parser.parse_args(argv)
    day = args.date or yesterday()
    with scheduled_job("agent-core-answer-sample"):
        print(describe(asyncio.run(run_sample(day)), day))


if __name__ == "__main__":
    main()
