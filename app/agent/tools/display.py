"""How a person is told about the tools several domains share (`ToolDisplay`, specs/025), and the helpers each domain's own `display.py` writes with.

A summary reads the tool's OWN result string, which this codebase wrote: a pattern that stops matching yields no summary, never a wrong one
(`tests/agent/tools/test_display.py` holds the patterns to what the tools really return).
"""
import re
from collections.abc import Callable, Mapping
from typing import Any

from app.core.untrusted import OPEN_TAG
from app.ports.outbound.domains import Effect, ToolDisplay


def arg(args: Mapping[str, Any], name: str) -> str:
    """One argument as text. The arguments are the model's, shown BEFORE the tool validates them, so a missing or odd one must not fail a pause."""
    value = args.get(name)
    value = getattr(value, "value", value)  # an enum member shows its value
    return "?" if value is None or value == "" else str(value)


def matched(pattern: str, template: str) -> Callable[[str], str | None]:
    """A summary from a result that matches `pattern`: `template` filled with its groups."""
    compiled = re.compile(pattern)

    def summarize(result: str) -> str | None:
        found = compiled.search(result)
        return template.format(*found.groups()) if found else None

    return summarize


def counted(count: int) -> str:
    return "No results" if not count else f"{count} result" if count == 1 else f"{count} results"


def _document_summary(result: str) -> str | None:
    if result.startswith("No relevant documents found"):
        return counted(0)
    if not result.startswith(OPEN_TAG):
        return None
    count = 0
    while re.search(rf"(?m)^\[{count + 1}\] ", result):  # the markers are numbered from 1; a number inside a passage does not extend the run
        count += 1
    return counted(count) if count else None


SHARED_TOOL_DISPLAY: dict[str, ToolDisplay] = {
    "search_docs": ToolDisplay("Search the knowledge base", summarize=_document_summary),
    "calculator": ToolDisplay("Calculate"),
    "ask_clarification": ToolDisplay("Ask you a question"),
    "skill_search": ToolDisplay("Look for a skill"),
    "use_skill": ToolDisplay("Follow a skill"),
    "run_subagent": ToolDisplay("Ask a specialist", describe=lambda args: Effect(f"Hands this task to a specialist: {arg(args, 'task')}", reversible=True)),
}
