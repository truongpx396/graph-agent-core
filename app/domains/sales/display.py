"""How a person is told about the sales domain's own tools (`ToolDisplay`, specs/025)."""
from app.agent.tools.display import arg, matched
from app.domains.shared.display import page_fetch
from app.ports.outbound.domains import Effect, ToolDisplay

_CRM, _TEAM = "CRM", "Sales team"

TOOL_DISPLAY: dict[str, ToolDisplay] = {
    "log_lead_interaction": ToolDisplay(
        "Log a lead interaction",
        describe=lambda args: Effect(f"Records for {arg(args, 'name')} ({arg(args, 'contact')}): {arg(args, 'notes')}", target=_CRM, visible_to=_TEAM),
        summarize=matched(r"^Logged interaction for lead #(\d+)", "Logged for lead #{}"),
    ),
    "schedule_followup": ToolDisplay(
        "Schedule a follow-up",
        describe=lambda args: Effect(
            f"Schedules a follow-up with {arg(args, 'contact')} in {arg(args, 'due_in_days')} days: {arg(args, 'note')}", target=_CRM, visible_to=_TEAM
        ),
        summarize=matched(r"^Follow-up #\d+ scheduled for (\d{4}-\d{2}-\d{2})", "Follow-up scheduled for {}"),
    ),
    "handoff_to_human": ToolDisplay(
        "Hand a lead to a sales rep",
        describe=lambda args: Effect(
            f"Hands {arg(args, 'contact')} to a person ({arg(args, 'reason')}): {arg(args, 'brief_summary')}", target="Sales team channel", visible_to=_TEAM
        ),
    ),
    "mark_lead_lost": ToolDisplay(
        "Mark a lead lost",
        describe=lambda args: Effect(
            f"Marks {arg(args, 'contact')} lost ({arg(args, 'reason')}) and cancels its pending follow-ups", target=_CRM, visible_to=_TEAM
        ),
    ),
    "enrich_lead_from_website": ToolDisplay(
        "Research a lead's website",
        describe=lambda args: page_fetch(args, then=f" and adds what it finds to {arg(args, 'contact')}'s notes"),
    ),
    "package_lead_brief": ToolDisplay("Prepare a lead brief"),
    "list_pending_followups": ToolDisplay("List pending follow-ups"),
}
