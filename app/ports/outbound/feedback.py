"""A person's rating of one answer: where it is kept (PRODUCTION_REVIEW.md R28).

Advisory by design (Constitution Principle VII, on judged signals): a rating feeds a counter and a reviewer's reading, and no decision the agent makes. The rules (the
caller must own the conversation, what identifies an answer, how long a comment may be) are the application's (`app/agent/runtime/feedback.py`); this is only where the rows
live (`app/adapters/outbound/postgres/feedback.py`). Every method takes the tenant and person: a row is never read or written without them. Methods raise when the store
cannot be reached.
"""
from __future__ import annotations

from typing import Protocol

from app.ports.slot import Slot


class FeedbackStore(Protocol):
    async def record(
        self,
        *,
        tenant: str,
        principal: str,
        domain: str,
        thread_id: str,
        answer_ref: str,
        rating: str,
        comment: str | None,
        request_id: str | None,
    ) -> bool:
        """Stores this person's rating of one answer and returns True when it is a NEW rating, False when it replaced the same person's earlier one of that answer
        (their `comment` and `rating` are overwritten; their `request_id` is kept if the new one names none). `rating` is `up` or `down`."""
        ...


feedback_store: Slot[FeedbackStore] = Slot("feedback_store", FeedbackStore)
