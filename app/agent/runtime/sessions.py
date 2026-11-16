"""Session directory for the web UI's session switcher — lets a user list
and switch between their own past conversation threads.

Where the rows live is the `SessionStore` port (`app/ports/outbound/sessions.py`; the Postgres adapter explains why it is its own table).

Every row is tenant+principal scoped (SecurityCtx) and, since pattern 49,
domain-scoped too: a session belongs to whichever domain actually ran on
it, since resuming under a DIFFERENT domain would run that domain's
tools/prompt against history never built around them (see
`adapters/outbound/redis/queue.py::publish_request` for the same hazard). `domain`
defaults to `"ecorp"` and is set ONLY on first insert — immutable after,
like `title`.
"""
import logging
from typing import Any

from app.core.security import SecurityCtx, valid_ctx
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.jobs import turn_queue
from app.ports.outbound.sessions import session_store

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 60

# Thread ids the Telegram channel derives itself (`app/adapters/inbound/channels/telegram/bot.py::
# _thread_id` — `telegram:<person>:<chat id>`, the person being the caller's own id).
# Reserved: a caller may claim one only if it names THEM (app/adapters/inbound/api/routers/chat.py::
# _require_conversation_owner), or they could squat a real chat user's conversation
# before that user's first message.
TELEGRAM_THREAD_PREFIX = "telegram:"


def _display_title(title: str | None) -> str:
    """The stored title: stripped, defaulted, and cut to `TITLE_MAX_CHARS`.
    One implementation for the two writers (`upsert_session`, `claim_session`)
    so a session's title doesn't depend on which of them created the row."""
    display_title = (title or "New conversation").strip() or "New conversation"
    if len(display_title) > TITLE_MAX_CHARS:
        display_title = display_title[:TITLE_MAX_CHARS].rstrip() + "…"
    return display_title


async def upsert_session(
    ctx: SecurityCtx | None, thread_id: str, title: str | None = None, domain: str = "ecorp"
) -> None:
    """Best-effort write-through at the start of every turn
    (`app/agent/runtime/stream.py::astream_events_turn`, right after seeding) — NOT gated
    on the turn completing, unlike the turn metrics, so a
    rejected/moderated/short-circuited turn still shows up in the session
    list.

    `title`/`domain` are set ONLY on first insert; later turns only
    refresh `last_active_at`, so both stay fixed at the opening turn's
    values. A failing write must not fail the turn — same
    degrade-don't-crash posture as the usage events' write.
    """
    if not valid_ctx(ctx) or not thread_id:
        return
    try:
        await session_store.get().touch(ctx["tenant"], ctx["principal"], thread_id, _display_title(title), domain)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "chat session upsert failed; continuing without recording",
            extra={"error_class": type(exc).__name__},
        )


async def claim_session(
    ctx: SecurityCtx | None, thread_id: str, title: str | None = None, domain: str = "ecorp"
) -> bool:
    """The ownership gate for sending a message: claim `thread_id` for `ctx` if nobody has it, then report
    whether `ctx` owns it: `True` for the first claimant and the owner on every later call, `False` for anyone
    else (another tenant, principal or domain) and for an invalid ctx.

    Conversation state (checkpoint, cancel flag, thread lock, submission-dedup key) is keyed by `thread_id`
    alone and the id is client-supplied, so without a gate whoever names an id continues that conversation.
    `upsert_session`, the per-turn bookkeeping, is `ON CONFLICT DO UPDATE` and never asked who owned the row.
    The store makes the claim atomic: of two racers for a new id the loser waits for the winner, then sees its row.

    Unlike `upsert_session` this does NOT swallow a store failure: that one is best-effort and must never fail
    a turn, but this is an authorization decision, and when it can't be made the answer is "no" (the caller
    sees the exception, a 500, not a default-allow).
    """
    if not valid_ctx(ctx) or not thread_id:
        return False
    return await session_store.get().claim(ctx["tenant"], ctx["principal"], thread_id, _display_title(title), domain)


async def list_sessions(ctx: SecurityCtx | None, domain: str = "ecorp") -> list[dict[str, Any]]:
    """Every session for ctx's tenant+principal AND `domain`, most
    recently active first. Owner-level isolation (like `Policy.lower` for
    memories, not tenant-shared documents), with domain as a third
    scoping axis: the same principal's "support" and "sales" conversations
    never appear in each other's switcher."""
    if not valid_ctx(ctx):
        return []
    return await session_store.get().list(ctx["tenant"], ctx["principal"], domain)


async def session_belongs_to(ctx: SecurityCtx | None, thread_id: str, domain: str = "ecorp") -> bool:
    """Ownership check, without claiming: GET /chat/sessions/{thread_id}/
    messages and `.../pending_approval`, and `POST /chat/resume` and
    `/chat/cancel` — all of which only ever act on a conversation that
    already exists. (Sending a message uses `claim_session`, which creates
    the row for a new id.) `runtime/stream.py::get_session_messages` reads
    the shared Postgres checkpointer directly, which has no tenant/
    principal/domain to check, so the CALLER must verify ownership here
    first. A targeted row lookup, not a full `list_sessions` scan. Requiring `domain` to match
    too means a thread opened under a different domain reads as "not
    found," same as a different tenant's or principal's thread."""
    if not valid_ctx(ctx) or not thread_id:
        return False
    return await session_store.get().owns(ctx["tenant"], ctx["principal"], thread_id, domain)


class SessionBusyError(Exception):
    """A turn is running on the conversation, so it cannot be deleted now (`ErrorCode.THREAD_BUSY` at the API)."""


async def delete_session(ctx: SecurityCtx | None, thread_id: str, domain: str = "ecorp") -> bool:
    """Deletes one of the caller's own conversations: its checkpoints and its row in the session directory (PRODUCTION_REVIEW.md R11, the owner's side of
    `retention.py`). `False` when there is no such conversation for this tenant, person and domain, which reads the same as an unknown id: another person's
    thread is never confirmed to exist. Raises `SessionBusyError` while a turn holds the conversation's lock.

    Ownership is checked FIRST: the checkpointer has no tenant, person or domain to scope by, so nothing may reach `delete_thread` for an id that
    `session_belongs_to` did not clear. Checkpoints go before the row, so a failure between the two leaves a row the owner can delete again.

    A message sent in the moments between the busy check and the delete would run on a conversation whose checkpoints were just deleted: it starts a new one
    under the same id. The lock is the workers' to take, so this route does not hold it; the window is those moments, not the turn."""
    if not valid_ctx(ctx) or not await session_belongs_to(ctx, thread_id, domain):
        return False
    if await turn_queue.get().is_thread_busy(thread_id):
        raise SessionBusyError(thread_id)
    await checkpointing.get().delete_thread(thread_id)
    await session_store.get().delete(ctx["tenant"], ctx["principal"], thread_id, domain)
    return True
