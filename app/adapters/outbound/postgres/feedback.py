"""The `FeedbackStore` adapter (`app/ports/outbound/feedback.py`) over the appdata Postgres: the `chat_feedback` table (postgres-init/26-chat-feedback.sql).

Statements only: whether a caller may rate a conversation is the application's call (`app/agent/runtime/feedback.py`)."""
from app.platform.sql_store import get_connection


class PgFeedbackStore:
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
        # `xmax = 0` is how Postgres says a row was INSERTED by this statement rather than updated through ON CONFLICT: one round trip for the upsert and the answer.
        async with get_connection() as conn:
            cur = await conn.execute(
                "INSERT INTO chat_feedback (tenant, principal, thread_id, answer_ref, domain, request_id, rating, comment) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant, principal, thread_id, answer_ref) DO UPDATE SET "
                "rating = EXCLUDED.rating, comment = EXCLUDED.comment, "
                "request_id = COALESCE(EXCLUDED.request_id, chat_feedback.request_id), updated_at = now() "
                "RETURNING (xmax = 0)",
                (tenant, principal, thread_id, answer_ref, domain, request_id, rating, comment),
            )
            row = await cur.fetchone()
            return bool(row and row[0])
