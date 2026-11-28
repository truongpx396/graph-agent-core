"""The `tools` node's own announcements: what each call is, as it starts, and what became of it, as it ends (specs/025, GRAPH_PATTERNS.md pattern 118).

Why an override and not `astream_events`' tool callbacks: `on_tool_start` carries no tool call id, so two parallel calls of one tool cannot be paired, and a
tool that RAISES emits no end event at all, so a client showed it running until the answer. `_arun_one` is private API of `langgraph.prebuilt`;
`tests/agent/graph/test_tool_node.py` fails if an upgrade stops calling it.
"""
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.messages import ToolCall
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.prebuilt import ToolNode
from langgraph.types import Command

from app.agent.cost import metering
from app.agent.graph.loop_guards import tool_capability
from app.agent.graph.stream_events import STREAM_EVENT
from app.agent.graph.tool_display import (
    label_of,
    outcome_of,
    result_message,
    summary_of,
)
from app.core.errors import ErrorKind
from app.ports.outbound.domains import ToolDisplay


class AnnouncingToolNode(ToolNode):
    def __init__(self, tools: Sequence[BaseTool], *, capabilities: Mapping[str, str], display: Mapping[str, ToolDisplay], **kwargs: Any) -> None:
        super().__init__(tools, **kwargs)
        self._capabilities, self._display = capabilities, display

    async def _arun_one(self, call: ToolCall, input_type: Any, config: RunnableConfig) -> Any:
        name, call_id = call["name"], call["id"]
        await adispatch_custom_event(
            STREAM_EVENT,
            {
                "type": "tool_start",
                "tool": name,
                "args": call["args"],
                "call_id": call_id,
                "label": label_of(name, self._display),
                "tier": tool_capability(name, self._capabilities),
                "at": datetime.now(UTC).isoformat(timespec="milliseconds"),
            },
            config=config,
        )
        started = time.monotonic()
        # A delegated run that fails has spent money its state never hands back: the turn's total is then unknown, not smaller.
        with metering.lost_usage() as lost:
            output = await super()._arun_one(call, input_type, config)
        message = result_message(output, call_id)
        outcome = outcome_of(message)
        end: dict[str, Any] = {"type": "tool_end", "tool": name, "call_id": call_id, "outcome": outcome, "duration_ms": int((time.monotonic() - started) * 1000)}
        if outcome == "refused":
            end["kind"] = ErrorKind.BLOCKED.value
        if outcome == "ok" and (summary := summary_of(name, message, self._display)):
            end["summary"] = summary
        await adispatch_custom_event(STREAM_EVENT, end, config=config)
        if lost and message is not None and not isinstance(output, Command):
            return Command(update={"messages": [message], "turn_usage": [metering.UNKNOWN_USAGE]})
        return output
