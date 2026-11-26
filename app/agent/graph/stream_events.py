"""Nodes that tell the stream runtime what the client should see, instead of the runtime recognising them by name.

`runtime/stream.py::_run_graph_stream` used to decide what to send by matching `on_chain_end` events for `retry_output`, `check_output`, `retry_exhausted` and
`compact_history` and reading their outputs. Renaming a node, or adding one that also replaces the answer, silently changed what the client saw. Now a node
ANNOUNCES the event itself, with `adispatch_custom_event`, and the runtime forwards every announcement (one branch, no node names).

Not an `async def` rewrite of the checks: `check_output` and `retry_output` are pure functions of the state that a hundred unit tests call directly, and an
`async def` returns a coroutine however its `config` is defaulted. They stay as they are, and `announcing` wraps one at registration (`build_graph`) in a node
that runs it, then announces what its update means. PRODUCTION_REVIEW.md R35, part A.

What is announced, each as `{"type": ...}` (the shapes `_run_graph_stream` already sent):
  - `retry`: what the client has shown so far is to be discarded (the answer was rejected or replaced);
  - `token` (after a `retry`): the replacement text, when no model call will follow to stream a new one;
  - `compacted`: older context was trimmed before the model ran;
  - `hold` (from `agent` itself, before its model call) and `release` (check_output: the answer stands): keep the coming tokens back, then send them
    (`runtime/held_answer.py`). Neither is forwarded to a client.
"""
import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.callbacks.manager import adispatch_custom_event
from langchain_core.runnables import RunnableConfig

from app.agent.graph.state import CONTEXT_GAP_REASON

# The one custom event name; `_run_graph_stream` forwards every event of this name that does not come from a nested subagent run.
STREAM_EVENT = "agent_stream"

Announce = Callable[[dict[str, Any]], list[dict[str, Any]]]


def replaced_the_answer(update: dict[str, Any]) -> list[dict[str, Any]]:
    """`retry`, then the replacement text as a `token`, when `update` replaced the last message in place (check_output after it inserted a citation marker,
    retry_exhausted after it gave up). No events when it left the answer alone."""
    messages = update.get("messages") or []
    if not messages:
        return []
    events: list[dict[str, Any]] = [{"type": "retry"}]
    text = messages[-1].content
    if isinstance(text, str) and text:
        events.append({"type": "token", "content": text})
    return events


def judged_the_answer(update: dict[str, Any]) -> list[dict[str, Any]]:
    """check_output's: what `replaced_the_answer` says, `release` when the answer stands as written (anything held back goes out now, not when the turn
    ends), and nothing at all for one `context_gap` is about to take back (it may have been repaired on the way, and sending that would put on screen
    exactly what the stream is holding back: pattern 105)."""
    if update.get("last_retry_reason") == CONTEXT_GAP_REASON:
        return []
    return replaced_the_answer(update) or ([] if update.get("last_retry_reason") else [{"type": "release"}])


def rejected_the_answer(_update: dict[str, Any]) -> list[dict[str, Any]]:
    """`retry_output` always means: discard what was streamed, a fresh answer follows from `agent`."""
    return [{"type": "retry"}]


def trimmed_the_history(update: dict[str, Any]) -> list[dict[str, Any]]:
    """`compacted`, only when `compact_history` really trimmed something (its update is empty otherwise)."""
    return [{"type": "compacted"}] if update.get("messages") else []


def announcing(node: Callable[..., Any], announce: Announce) -> Callable[..., Awaitable[dict[str, Any]]]:
    """An async node that runs `node` (sync or async) and then announces `announce(update)`.

    Built by hand and not with `functools.wraps`: that would expose `node`'s own signature through `__wrapped__`, and LangGraph decides whether to pass
    `config` from the signature it sees. It passes `config` ON to a node that takes one (`compact_history` meters its model call from it: a wrapper that
    dropped it recorded no usage for compaction, which a metering test caught). Announced BEFORE the update is returned, so the client sees the `retry` ahead
    of whatever the next node streams. With no `config` (a direct call in a test) nothing is dispatched: there is no run to announce to."""
    takes_config = "config" in inspect.signature(node).parameters

    async def announcing_node(state: Any, config: RunnableConfig | None = None) -> dict[str, Any]:
        update = node(state, config) if takes_config else node(state)
        if inspect.isawaitable(update):
            update = await update
        if config is not None:
            for event in announce(update):
                await adispatch_custom_event(STREAM_EVENT, event, config=config)
        return update  # type: ignore[no-any-return]  # a node's update is a dict by contract

    announcing_node.__name__ = getattr(node, "__name__", "announcing_node")
    return announcing_node
