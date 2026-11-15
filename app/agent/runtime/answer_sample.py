"""Sample yesterday's answers and run the grounded-claims checks over them (PRODUCTION_REVIEW.md R28).

There is a strong offline tier (`make eval`, promptfoo, the model evaluation harness) and nothing online: nobody learns how often a real answer cites a source that was
never offered, or leans on one without saying so. The checks already exist and are pure (`count_ungrounded_claims`, `find_likely_uncited`, `find_likely_misattributed`, run on
every turn by `check_output`); this runs them over a random sample of the previous day's conversations, from what was saved, and exports counts. No model is called.

Advisory, as Constitution Principle VII says of every judged signal: the findings are heuristics tuned on small models, so they are a trend to watch, never a verdict, and nothing
alerts on them.

## What it reads, and what it emits

For each sampled conversation it reads the LAST turn's final answer and the citations that turn was offered, from the checkpoint. It reads across tenants (the sample is an
operator's, like the retention sweep, and no request may reach it), so it emits COUNTS ONLY: no answer text, no citation text, no tenant, person or thread id leaves this
function, in a metric or a log line. `ANSWER_SAMPLE_TENANTS` limits it to tenants that agree.

A conversation counts as the day's if its last message was that day (`last_active_at`); one that was active again today is judged by tomorrow's run, on its newer answer.
"""
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage

from app.agent.graph.citations import (
    count_ungrounded_claims,
    find_likely_misattributed,
    find_likely_uncited,
    find_used_citations,
)
from app.agent.graph.messages import is_user_message
from app.core import metrics
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.sessions import session_store

logger = logging.getLogger(__name__)

FINDINGS = ("ungrounded_marker", "uncited", "misattributed")


@dataclass
class SampleResult:
    sampled: int = 0
    judged: int = 0
    no_citations: int = 0
    unreadable: int = 0
    findings: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FINDINGS, 0))


def final_answer(messages: list[BaseMessage]) -> str | None:
    """The text of the last turn's answer: the last message, if it is the model's, has no tool calls and says something, and comes after the person's last message.
    Anything else (the conversation ended on a pause, a tool round or an error) has no answer to judge."""
    if not messages or not isinstance(messages[-1], AIMessage):
        return None
    last = messages[-1]
    if last.tool_calls or not isinstance(last.content, str) or not last.content.strip():
        return None
    if not any(is_user_message(m) for m in messages[:-1]):
        return None
    return last.content


def judge(answer: str, citations: list[dict[str, Any]]) -> list[str]:
    """Which of `FINDINGS` this answer shows. The same three functions `check_output` runs on every turn, over the same inputs; an empty list is a clean answer."""
    used = find_used_citations(answer, citations)
    found = []
    if count_ungrounded_claims(answer, citations) > 0:
        found.append("ungrounded_marker")
    if find_likely_uncited(answer, citations, used):
        found.append("uncited")
    if find_likely_misattributed(answer, citations, used):
        found.append("misattributed")
    return found


def day_window(day: date) -> tuple[datetime, datetime]:
    """`[start, end)` of `day` in UTC."""
    start = datetime.combine(day, time.min, tzinfo=timezone.utc)  # noqa: UP017 - datetime.UTC is 3.11+, and this reads the same everywhere
    return start, start + timedelta(days=1)


async def sample_answers(*, day: date, size: int, tenants: tuple[str, ...] = ()) -> SampleResult:
    """Reads up to `size` random conversations that were last active on `day` and judges each one's final answer. The checkpointer must be open (the job opens it).
    A conversation that cannot be read counts as `unreadable` and does not stop the run."""
    start, end = day_window(day)
    rows = await session_store.get().sample_active(start, end, size, tenants)
    saver = await checkpointing.get().open()
    result = SampleResult()
    for row in rows:
        result.sampled += 1
        try:
            saved = await saver.aget_tuple({"configurable": {"thread_id": row["thread_id"], "checkpoint_ns": ""}})
            values = saved.checkpoint["channel_values"] if saved else {}
            answer = final_answer(list(values.get("messages") or []))
        except Exception as exc:  # noqa: BLE001 - one unreadable conversation must not stop a sample of fifty; it is counted
            logger.warning("answer_sample_unreadable", extra={"error_class": type(exc).__name__})
            answer, values = None, {}
        if answer is None:
            result.unreadable += 1
            metrics.agent_answer_sample_total.labels(outcome="unreadable").inc()
            continue
        citations = list(values.get("citations") or [])
        if not citations:
            result.no_citations += 1
            metrics.agent_answer_sample_total.labels(outcome="no_citations").inc()
            continue
        result.judged += 1
        metrics.agent_answer_sample_total.labels(outcome="judged").inc()
        for finding in judge(answer, citations):
            result.findings[finding] += 1
            metrics.agent_answer_sample_finding_total.labels(finding=finding).inc()
    return result
