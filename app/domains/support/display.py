"""How a person is told about the support domain's own tools (`ToolDisplay`, specs/025). Nothing here can be taken back: the domain has no tool that
deletes a ticket or a comment, or that undoes an escalation."""
from collections.abc import Mapping
from typing import Any

from app.agent.tools.display import arg, matched
from app.domains.shared.display import page_fetch
from app.ports.outbound.domains import Effect, ToolDisplay

_QUEUE, _TEAM = "Support queue", "Support team"


def _new_ticket(args: Mapping[str, Any]) -> Effect:
    also = f", and escalates it to a person: {arg(args, 'escalation_reason')}" if args.get("escalation_reason") else ""
    return Effect(f'Opens ticket "{arg(args, "subject")}" at {arg(args, "priority")} priority{also}', target=_QUEUE, visible_to=_TEAM)


TOOL_DISPLAY: dict[str, ToolDisplay] = {
    "create_ticket": ToolDisplay("Create a support ticket", describe=_new_ticket, summarize=matched(r"^Ticket #(\d+) opened", "Ticket #{} created")),
    "escalate_to_human": ToolDisplay(
        "Escalate a ticket to a person",
        describe=lambda args: Effect(f"Escalates ticket #{arg(args, 'ticket_id')}: {arg(args, 'reason')}", target=_QUEUE, visible_to=_TEAM),
        summarize=matched(r"^Ticket #(\d+) escalated", "Ticket #{} escalated"),
    ),
    "add_ticket_comment": ToolDisplay(
        "Add a comment to a ticket",
        describe=lambda args: Effect(f'Adds to ticket #{arg(args, "ticket_id")}: "{arg(args, "comment")}"', target=_QUEUE, visible_to=_TEAM),
        summarize=matched(r"^Added your follow-up to ticket #(\d+)", "Comment added to ticket #{}"),
    ),
    "fetch_external_reference": ToolDisplay("Read an outside web page", describe=page_fetch),
    "check_ticket_status": ToolDisplay("Check a ticket"),
    "list_my_tickets": ToolDisplay("List your tickets"),
}
