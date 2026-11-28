"""The facts about a tool call that every client renders (specs/025): its label, tier and effect, the identity of an approval pause, and what became of
a call. All of it is computed here from the domain's `ToolDisplay` and capabilities, so no client parses arguments or guesses a risk.

Text a call put into the words (the model's arguments, a tool's result) is scrubbed and bounded. A `describe` or `summarize` that raises costs the
words, never the turn or the pause.
"""
import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from langchain_core.messages import ToolCall, ToolMessage
from langgraph.types import Command

from app.agent.graph.loop_guards import tool_capability
from app.core.scrubbing import scrub
from app.core.tool_runtime import NO_CTX_REFUSAL
from app.ports.outbound.domains import Effect, ToolDisplay

logger = logging.getLogger(__name__)

EFFECT_CHARS = 400  # enough to see what will happen, not enough for a pasted document to fill the card
FIELD_CHARS = 120  # a target, an audience, a summary line

_UNDESCRIBED = Effect("Runs with the details below")
_UNDESCRIBED_READ = Effect("Looks something up. Nothing is changed", reversible=True)


def bounded(text: str, limit: int) -> str:
    """Scrubbed, one line, at most `limit` characters."""
    flat = " ".join(scrub(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def label_of(name: str, display: Mapping[str, ToolDisplay]) -> str:
    """The tool's label; for a name without one (a model's invention, a plugin that declares none), the name as words, never as an identifier."""
    entry = display.get(name)
    return entry.label if entry else bounded(name.replace("_", " ").capitalize(), FIELD_CHARS)


def _effect(call: ToolCall, tier: str, display: Mapping[str, ToolDisplay]) -> Effect:
    entry = display.get(call["name"])
    if entry is not None and entry.describe is not None:
        try:
            return entry.describe(call["args"])
        except Exception as exc:  # noqa: BLE001 - words for a card: the person still sees the arguments, and the call still needs approval
            logger.warning("tool_describe_failed", extra={"tool": call["name"], "error_class": type(exc).__name__})
    return _UNDESCRIBED_READ if tier == "read_only" else _UNDESCRIBED


def approval_id(tool_calls: list[ToolCall]) -> str:
    """The identity of one pause: of exactly the calls and arguments it asks about. Derived, not random, because LangGraph runs the pausing node again
    on resume, and a call the model proposes anew has a new call id, so an answer to an earlier pause can never match a later one."""
    asked = json.dumps([[tc["id"], tc["name"], tc["args"]] for tc in tool_calls], sort_keys=True, default=str)
    return f"apr_{hashlib.sha256(asked.encode()).hexdigest()[:32]}"


def approval_items(tool_calls: list[ToolCall], capabilities: Mapping[str, str], display: Mapping[str, ToolDisplay]) -> list[dict[str, Any]]:
    """One item per pending call, in the calls' order (`ApprovalItem`)."""
    items = []
    for call in tool_calls:
        tier = tool_capability(call["name"], capabilities)
        effect = _effect(call, tier, display)
        items.append(
            {
                "call_id": call["id"],
                "tier": tier,
                "title": label_of(call["name"], display),
                "effect": bounded(effect.text, EFFECT_CHARS),
                "target": bounded(effect.target, FIELD_CHARS) if effect.target else None,
                "visible_to": bounded(effect.visible_to, FIELD_CHARS) if effect.visible_to else None,
                "reversible": effect.reversible,
            }
        )
    return items


def approval_request(tool_calls: list[ToolCall], *, capabilities: Mapping[str, str], display: Mapping[str, ToolDisplay]) -> dict[str, Any]:
    """What a pause carries besides the calls: its identity and the facts about each call. Stored with the interrupt, so a reloaded page reads the same card."""
    return {"approval_id": approval_id(tool_calls), "items": approval_items(tool_calls, capabilities, display)}


def result_message(output: object, call_id: str | None) -> ToolMessage | None:
    """The ToolMessage a call produced, whether the tool node returned it or wrapped it in a `Command` (run_subagent)."""
    if isinstance(output, ToolMessage):
        return output
    if isinstance(output, Command) and isinstance(output.update, dict):
        return next((m for m in output.update.get("messages", []) if isinstance(m, ToolMessage) and m.tool_call_id == call_id), None)
    return None


def outcome_of(message: ToolMessage | None) -> str:
    """`error` (it raised, or its arguments were invalid), `refused` (it said no: no usable security context, or not on the domain's allowlist), else `ok`."""
    if message is None:
        return "ok"
    if message.status == "error":
        return "error"
    return "refused" if message.content == NO_CTX_REFUSAL else "ok"


def summary_of(name: str, message: ToolMessage | None, display: Mapping[str, ToolDisplay]) -> str | None:
    entry = display.get(name)
    if entry is None or entry.summarize is None or message is None or not isinstance(message.content, str):
        return None
    try:
        summary = entry.summarize(message.content)
    except Exception as exc:  # noqa: BLE001 - a summary is a nicety: the call and its outcome are reported without it
        logger.warning("tool_summary_failed", extra={"tool": name, "error_class": type(exc).__name__})
        return None
    return bounded(summary, FIELD_CHARS) if summary else None
