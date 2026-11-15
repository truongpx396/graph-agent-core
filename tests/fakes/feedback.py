"""A fake for `FeedbackStore` (`app/ports/outbound/feedback.py`), with the primary key's meaning: one rating per tenant, person, thread and answer, replaced by a later one."""
from typing import Any


class InMemoryFeedbackStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.fail: Exception | None = None

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
        if self.fail is not None:
            raise self.fail
        key = (tenant, principal, thread_id, answer_ref)
        previous = self.rows.get(key)
        self.rows[key] = {
            "domain": domain, "rating": rating, "comment": comment,
            "request_id": request_id if request_id is not None or previous is None else previous["request_id"],
        }
        return previous is None
