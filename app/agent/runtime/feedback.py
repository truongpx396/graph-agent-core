"""A person rates one answer (PRODUCTION_REVIEW.md R28).

There was a strong OFFLINE tier (`make eval`, promptfoo, the model evaluation harness) and nothing from production. A thumbs up or down, with an optional comment, is the
cheapest signal a person can give. It is advisory, as Constitution Principle VII says of every judged signal: it is counted (`agent_feedback_total{rating}`) and stored for
a reviewer to read, and nothing the agent does ever depends on it.

The caller must own the conversation (`sessions.session_belongs_to`, the same boundary as every route about a thread), so a person cannot rate, or probe for, another
person's conversation: it reads as not found. The comment is stored, never logged: it is free text from a person and may say anything. Attaching a rating to the trace as a
score waits for traces in production (R17 part 2), which this repository does not have yet.
"""
from typing import Literal

from app.agent.runtime import sessions
from app.core import metrics
from app.core.security import SecurityCtx, valid_ctx
from app.ports.outbound.feedback import feedback_store

COMMENT_MAX_CHARS = 1000


def answer_ref(*, message_index: int | None, request_id: str | None) -> str:
    """What identifies the answer: its place in the transcript when the client knows it (what `GET /chat/sessions/{id}/messages` numbers), else the request that made it.
    Exactly one is used, so rating the same answer both ways is still one row, not two."""
    if message_index is not None:
        return f"message:{message_index}"
    if request_id:
        return f"request:{request_id}"
    raise ValueError("an answer is identified by its message_index or its request_id")


async def record_answer_feedback(
    ctx: SecurityCtx | None,
    thread_id: str,
    domain: str,
    *,
    rating: Literal["up", "down"],
    comment: str | None = None,
    request_id: str | None = None,
    message_index: int | None = None,
) -> bool:
    """Stores the rating. `False` when the caller does not own the conversation (or the context is invalid): not found, and nothing is written. Raises `ValueError` when
    the answer is not identified. A second rating of the same answer by the same person replaces the first and is not counted again."""
    ref = answer_ref(message_index=message_index, request_id=request_id)
    if not valid_ctx(ctx) or not await sessions.session_belongs_to(ctx, thread_id, domain):
        return False
    text = (comment or "").strip()[:COMMENT_MAX_CHARS] or None
    is_new = await feedback_store.get().record(
        tenant=ctx["tenant"],
        principal=ctx["principal"],
        domain=domain,
        thread_id=thread_id,
        answer_ref=ref,
        rating=rating,
        comment=text,
        request_id=request_id,
    )
    if is_new:
        metrics.agent_feedback_total.labels(rating=rating).inc()
    return True
