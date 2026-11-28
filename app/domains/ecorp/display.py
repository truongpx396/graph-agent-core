"""How a person is told about Ecorp's own tools (`ToolDisplay`, specs/025)."""
from app.agent.tools.display import arg, matched
from app.ports.outbound.domains import Effect, ToolDisplay

TOOL_DISPLAY: dict[str, ToolDisplay] = {
    "add_note": ToolDisplay(
        "Add a note to the knowledge base",
        describe=lambda args: Effect(
            f'Adds the note "{arg(args, "title")}": {arg(args, "content")}',
            target=f"{arg(args, 'topic')} knowledge base",
            visible_to="Everyone in your organisation",
        ),
        summarize=matched(r"^Note '.*' added to the (\S+) knowledge base", "Added to the {} knowledge base"),
    ),
    "remember": ToolDisplay(
        "Remember a fact about you",
        describe=lambda args: Effect(f'Saves to your memory: "{arg(args, "content")}"', target="Your memory", visible_to="Only you"),
    ),
    "query_employees": ToolDisplay("Look up employees"),
}
