"""How a person is told about the ops domain's own tools (`ToolDisplay`, specs/025)."""
from app.agent.tools.display import arg, matched
from app.domains.shared.display import page_fetch
from app.ports.outbound.domains import Effect, ToolDisplay

_LOG, _TEAM = "Incident log", "Operations team"

TOOL_DISPLAY: dict[str, ToolDisplay] = {
    "post_to_team_channel": ToolDisplay(
        "Post to a team channel",
        describe=lambda args: Effect(f'Posts: "{arg(args, "message")}"', target=arg(args, "channel"), visible_to="Everyone in the channel"),
    ),
    "log_incident": ToolDisplay(
        "Log an incident",
        describe=lambda args: Effect(f'Records the incident "{arg(args, "summary")}": {arg(args, "detail")}', target=_LOG, visible_to=_TEAM),
        summarize=matched(r"^Incident #(\d+) logged", "Incident #{} logged"),
    ),
    "resolve_incident": ToolDisplay(
        "Resolve an incident",
        describe=lambda args: Effect(f"Marks incident #{arg(args, 'incident_id')} resolved: {arg(args, 'resolution')}", target=_LOG, visible_to=_TEAM),
        summarize=matched(r"^Incident #(\d+) resolved", "Incident #{} resolved"),
    ),
    "check_vendor_status_page": ToolDisplay("Check a vendor's status page", describe=page_fetch),
    "fetch_metrics_summary": ToolDisplay("Read the system metrics"),
    "list_recent_incidents": ToolDisplay("List recent incidents"),
}
