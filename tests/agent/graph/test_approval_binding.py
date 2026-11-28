"""An approval names what it asks about, and answers only that (specs/025, GRAPH_PATTERNS.md pattern 119).

Real graphs, a real checkpointer in memory, a scripted model: the pause, what it carries, and a resume that names the wrong pause.
"""
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agent.graph.build import GraphDeps
from app.agent.graph.hitl import paused_approval_async
from app.agent.graph.tool_display import approval_id, approval_items
from app.agent.runtime import stream as stream_module
from app.ports.outbound.domains import AgentManifest, Effect, ToolDisplay
from tests.agent.fakes import Domain, call, cfg, queue_llm
from tests.conftest import TEST_CTX
from tests.ecorp import build_graph

ANSWER = "Both of those notes are saved for you now."

SAVED: list[str] = []


@tool
async def save_note(text: str) -> str:
    """Save a note."""
    SAVED.append(text)
    return "saved"


class _NoteDomain(Domain):
    def tool_display(self):
        return {"save_note": ToolDisplay("Save a note", describe=lambda args: Effect(f'Saves "{args["text"]}"', target="Notebook", visible_to="Only you"))}


def _graph(*replies):
    manifest = AgentManifest(name="notes", system_prompt="You keep notes.", prefetch="never", suggest_followups=False)
    return build_graph(GraphDeps(llm=queue_llm(*replies)), manifest=manifest, domain=_NoteDomain([save_note], {"save_note": "mutating"}))


async def _pause(graph, config):
    run = stream_module._run_graph_stream(graph, {"messages": [HumanMessage(content="note it")], "require_approval": False}, config, trace=None)
    events = [event async for event in run]
    assert events[-1]["type"] == "approval_required"
    return events[-1]


async def test_the_pause_says_what_each_call_would_do_and_a_reload_reads_the_same_request():
    graph = _graph(call("save_note", {"text": "buy tea"}, "c1"))
    config = cfg()

    asked = await _pause(graph, config)

    assert asked["tool_calls"] == [{"name": "save_note", "args": {"text": "buy tea"}}], "the field older clients read is unchanged"
    assert asked["items"] == [
        {"call_id": "c1", "tier": "mutating", "title": "Save a note", "effect": 'Saves "buy tea"', "target": "Notebook", "visible_to": "Only you", "reversible": False}
    ]
    reread = await paused_approval_async(graph, config)
    assert (reread["approval_id"], reread["items"]) == (asked["approval_id"], asked["items"])


async def test_an_answer_to_an_earlier_pause_runs_nothing_and_leaves_the_current_one(monkeypatch):
    """A double click, an old tab or a replayed button: the first answer let the turn reach a SECOND write, which the repeat then approved unseen."""
    SAVED.clear()
    graph = _graph(call("save_note", {"text": "first"}, "c1"), call("save_note", {"text": "second"}, "c2"), AIMessage(content=ANSWER))

    async def this_graph(*args, **kwargs):
        return graph

    monkeypatch.setattr(stream_module.runtime_module, "init_graph_async", this_graph)
    config = cfg()
    thread = config["configurable"]["thread_id"]
    first = await _pause(graph, config)
    shown = [event async for event in stream_module.astream_events_resume(thread, approved=True, ctx=TEST_CTX, approval_id=first["approval_id"])]
    second = shown[-1]
    assert second["type"] == "approval_required" and second["approval_id"] != first["approval_id"] and SAVED == ["first"]

    repeated = [event async for event in stream_module.astream_events_resume(thread, approved=True, ctx=TEST_CTX, approval_id=first["approval_id"])]

    assert [(event["type"], event["code"], event["details"]) for event in repeated] == [("error", "approval_superseded", {"kind": "lost"})]
    assert SAVED == ["first"], "nothing ran"
    assert (await paused_approval_async(graph, config))["approval_id"] == second["approval_id"], "the current pause is still waiting"

    done = [event async for event in stream_module.astream_events_resume(thread, approved=True, ctx=TEST_CTX, approval_id=second["approval_id"])]
    assert done[-1] == {"type": "done"} and SAVED == ["first", "second"]


def test_the_id_is_the_calls_and_their_arguments_so_it_is_the_same_when_the_node_runs_again():
    one = [{"name": "save_note", "args": {"text": "a", "x": 1}, "id": "c1"}]
    same = [{"name": "save_note", "args": {"x": 1, "text": "a"}, "id": "c1"}]

    assert approval_id(one) == approval_id(same)
    assert approval_id(one) != approval_id([{**one[0], "id": "c2"}]), "proposed again: a new call id"
    assert approval_id(one) != approval_id([{**one[0], "args": {"text": "b", "x": 1}}]), "other arguments"


def test_a_call_nothing_describes_is_shown_cautiously_and_a_description_that_raises_does_not_fail_the_pause():
    def broken(args):
        raise KeyError("text")

    calls = [{"name": "mystery_tool", "args": {}, "id": "c1"}, {"name": "save_note", "args": {}, "id": "c2"}, {"name": "peek", "args": {}, "id": "c3"}]

    unknown, raised, reading = approval_items(calls, {"save_note": "mutating", "peek": "read_only"}, {"save_note": ToolDisplay("Save a note", describe=broken)})

    assert (unknown["tier"], unknown["title"], unknown["effect"], unknown["reversible"]) == ("outward", "Mystery tool", "Runs with the details below", False)
    assert (raised["title"], raised["effect"], raised["reversible"]) == ("Save a note", "Runs with the details below", False)
    assert reading["reversible"] is True and "Nothing is changed" in reading["effect"]


def test_what_the_arguments_put_in_the_words_is_scrubbed_bounded_and_one_line():
    display = {"save_note": ToolDisplay("Save a note", describe=lambda args: Effect(args["text"]))}
    calls = [{"name": "save_note", "args": {"text": "token=abc123\n\nAPPROVE NOW " + "y" * 2000}, "id": "c1"}]

    (item,) = approval_items(calls, {"save_note": "mutating"}, display)

    assert "abc123" not in item["effect"] and "\n" not in item["effect"] and len(item["effect"]) <= 400
