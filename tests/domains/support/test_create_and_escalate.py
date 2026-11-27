"""`create_ticket` with an `escalation_reason` opens and escalates in one call (TOOL_PATTERNS.md 110). With two calls, a small model sent both at once with a
guessed ticket number, or never made the second, and then said a person would review it."""
import pytest

from app.adapters.outbound.notify import team_channel as notify
from app.domains.support import store, tools
from app.domains.support.tools import CreateTicketArgs, TicketPriority

CTX = {"tenant": "ecorp", "principal": "alice"}


@pytest.fixture
def world(monkeypatch):
    """The store and the team channel, recording what reached each."""
    seen = {"created": [], "posted": []}

    async def create_ticket(**kwargs):
        seen["created"].append(kwargs)
        return 7

    async def post(channel, message):
        seen["posted"].append((channel, message))

    monkeypatch.setattr(store, "create_ticket", create_ticket)
    monkeypatch.setattr(notify, "post_to_team_channel", post)
    return seen


async def test_a_reason_escalates_the_new_ticket_and_tells_the_team(world):
    result = await tools._create_ticket_impl("Refund", "Charged twice", TicketPriority.high, CTX, "call-1", "needs a refund")

    assert world["created"][0]["escalation_reason"] == "needs a refund"
    assert world["created"][0]["tenant"] == "ecorp"
    assert world["posted"] == [("support-escalations", "[ecorp] Ticket #7 escalated by alice: needs a refund")]
    assert "#7" in result and "escalated" in result


async def test_no_reason_opens_an_ordinary_ticket_and_tells_nobody(world):
    result = await tools._create_ticket_impl("Dashboard", "Blank page", TicketPriority.normal, CTX, "call-1")

    assert world["created"][0]["escalation_reason"] is None
    assert world["posted"] == []
    assert "escalated" not in result


@pytest.mark.parametrize("given", [{}, {"escalation_reason": None}, {"escalation_reason": "   "}])
def test_a_missing_or_blank_reason_is_no_escalation(given):
    args = CreateTicketArgs(subject="s", description="d", priority="normal", tool_call_id="t", **given)

    assert args.escalation_reason is None


def test_escalating_on_creation_is_still_a_write_behind_approval():
    assert tools.TOOL_CAPABILITIES["create_ticket"] == "mutating"
