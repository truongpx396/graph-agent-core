"""How a person is told about the tools some domains share (`web_search`, the sandbox). Each domain's `display.py` merges what it exposes."""
from collections.abc import Mapping
from typing import Any

from app.agent.tools.display import arg, counted
from app.core.untrusted import OPEN_TAG
from app.domains.shared.web_search import NO_RESULTS
from app.ports.outbound.domains import Effect, ToolDisplay

_SANDBOX = "Isolated sandbox"


def page_fetch(args: Mapping[str, Any], *, then: str = "") -> Effect:
    """A tool that reads a page on someone else's server: the request cannot be taken back, and the site sees it."""
    return Effect(f"Fetches {arg(args, 'url')}{then}", target=arg(args, "url"), visible_to="The site's operator sees the request")


def _web_summary(result: str) -> str | None:
    if result == NO_RESULTS:
        return counted(0)
    return counted(sum(line.startswith("- ") for line in result.splitlines())) if result.startswith(OPEN_TAG) else None


WEB_SEARCH_DISPLAY: dict[str, ToolDisplay] = {
    "web_search": ToolDisplay(
        "Search the web",
        describe=lambda args: Effect(f'Sends the search "{arg(args, "query")}" to an outside search service', target="Outside search service"),
        summarize=_web_summary,
    ),
}

SANDBOX_DISPLAY: dict[str, ToolDisplay] = {
    "run_command_in_sandbox": ToolDisplay("Run a command in a sandbox", describe=lambda args: Effect(f"Runs: {arg(args, 'command')}", target=_SANDBOX)),
    "run_python_in_sandbox": ToolDisplay("Run Python in a sandbox", describe=lambda args: Effect(f"Runs this script: {arg(args, 'script')}", target=_SANDBOX)),
    "read_sandbox_file": ToolDisplay("Read a sandbox file", describe=lambda args: Effect(f"Reads {arg(args, 'path')}", target=_SANDBOX, reversible=True)),
    "write_sandbox_file": ToolDisplay(
        "Write a sandbox file",
        describe=lambda args: Effect(f"Writes {len(arg(args, 'content'))} characters to {arg(args, 'path')}, replacing what is there", target=_SANDBOX),
    ),
}
