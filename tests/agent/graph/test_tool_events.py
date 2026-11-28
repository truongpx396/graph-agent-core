"""What the stream says about a tool call (specs/025, GRAPH_PATTERNS.md pattern 118): its id, label, tier and what became of it.

A real graph with a scripted model, so the events are the ones the tools node announces; `tests/agent/conftest.py` holds each to the OpenAPI document.
`AnnouncingToolNode` overrides a private method of `langgraph.prebuilt.ToolNode`: if an upgrade stops calling it, these find no events.
"""
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from app.agent.graph.build import GraphDeps
from app.agent.runtime import stream as stream_module
from app.agent.tools.run_subagent import run_subagent_impl
from app.agent.tools.subagent_catalog import SubagentRecord
from app.core.tool_runtime import NO_CTX_REFUSAL
from app.ports.outbound.domains import AgentManifest, ToolDisplay
from tests.agent.fakes import Domain, call, cfg, queue_llm, scripted_llm
from tests.ecorp import build_graph

ANSWER = "I looked both of those things up for you just now."


@tool
async def look_up(topic: str) -> str:
    """Look a topic up."""
    if topic == "broken":
        raise RuntimeError("the index is down")
    if topic == "forbidden":
        return NO_CTX_REFUSAL
    return f"found {topic}"


class _DisplayedDomain(Domain):
    def __init__(self, tools, capabilities, display):
        super().__init__(tools, capabilities)
        self._display = display

    def tool_display(self):
        return dict(self._display)


def _graph(*replies, tools=(look_up,), display=None):
    display = display if display is not None else {"look_up": ToolDisplay("Look a topic up", summarize=lambda result: result.upper())}
    domain = _DisplayedDomain(list(tools), {t.name: "read_only" for t in tools}, display)
    manifest = AgentManifest(name="shelf", system_prompt="You look things up.", prefetch="never", suggest_followups=False)
    return build_graph(GraphDeps(llm=queue_llm(*replies)), manifest=manifest, domain=domain)


async def _events(graph):
    run = stream_module._run_graph_stream(graph, {"messages": [HumanMessage(content="look it up")]}, cfg(), trace=None)
    return [event async for event in run]


def _calls(*topics):
    return AIMessage(content="", tool_calls=[{"name": "look_up", "args": {"topic": topic}, "id": f"call-{topic}"} for topic in topics])


def _by_call(events, kind):
    return {event["call_id"]: event for event in events if event["type"] == kind}


async def test_parallel_calls_of_one_tool_are_paired_by_call_id_with_the_servers_label_tier_and_summary():
    events = await _events(_graph(_calls("tea", "coffee"), AIMessage(content=ANSWER)))

    starts, ends = _by_call(events, "tool_start"), _by_call(events, "tool_end")
    assert set(starts) == set(ends) == {"call-tea", "call-coffee"}
    for topic in ("tea", "coffee"):
        start, end = starts[f"call-{topic}"], ends[f"call-{topic}"]
        assert (start["tool"], start["args"], start["label"], start["tier"]) == ("look_up", {"topic": topic}, "Look a topic up", "read_only")
        assert (end["outcome"], end["summary"]) == ("ok", f"FOUND {topic.upper()}")
        assert end["duration_ms"] >= 0 and "kind" not in end and "subagent" not in end


async def test_a_call_that_raises_still_ends_and_a_refused_one_says_it_is_blocked():
    """A tool that raised emitted no end event at all: a client showed it running until the answer."""
    events = await _events(_graph(_calls("broken", "forbidden"), AIMessage(content=ANSWER)))

    ends = _by_call(events, "tool_end")
    assert (ends["call-broken"]["outcome"], ends["call-forbidden"]["outcome"], ends["call-forbidden"]["kind"]) == ("error", "refused", "blocked")
    assert not any("summary" in end for end in ends.values()), "nothing to sum up when it did not do the thing"
    assert events[-1] == {"type": "done"}, "a turn that recovers from either still ends normally"


async def test_a_summary_that_raises_costs_the_summary_and_nothing_else():
    def broken(result):
        raise KeyError("unexpected result")

    events = await _events(_graph(_calls("tea"), AIMessage(content=ANSWER), display={"look_up": ToolDisplay("Look up", summarize=broken)}))

    (end,) = _by_call(events, "tool_end").values()
    assert end["outcome"] == "ok" and "summary" not in end


async def test_what_a_result_puts_in_a_summary_is_scrubbed_and_bounded():
    display = {"look_up": ToolDisplay("Look up", summarize=lambda result: "password=hunter2 " + "x" * 500)}

    events = await _events(_graph(_calls("tea"), AIMessage(content=ANSWER), display=display))

    (end,) = _by_call(events, "tool_end").values()
    assert "hunter2" not in end["summary"] and len(end["summary"]) <= 120


async def test_a_subagents_own_calls_are_labelled_and_tagged_with_whose_they_are():
    record = SubagentRecord("researcher", "Looks things up.", "You look things up.", ("look_up",), None, ("ecorp",), Path("researcher/AGENT.md"))

    @tool
    async def delegate(task: str, config: RunnableConfig) -> str:
        """Hand a task to a specialist."""
        nested = queue_llm(call("look_up", {"topic": "tea"}, "nested-1"), AIMessage(content="Tea is grown in many places."))
        result = await run_subagent_impl(
            "researcher", task, config, domain="shelf", registry={"researcher": (record, ("look_up",))},
            tools_by_name={"look_up": look_up}, llm=nested, display={"look_up": ToolDisplay("Look a topic up")},
        )
        return result.answer

    graph = _graph(call("delegate", {"task": "tea"}, "outer-1"), AIMessage(content=ANSWER), tools=(delegate, look_up), display={"delegate": ToolDisplay("Delegate")})

    events = await _events(graph)

    starts = _by_call(events, "tool_start")
    assert (starts["nested-1"]["subagent"], starts["nested-1"]["label"]) == ("researcher", "Look a topic up")
    assert starts["outer-1"]["label"] == "Delegate" and "subagent" not in starts["outer-1"]
    assert _by_call(events, "tool_end")["nested-1"]["subagent"] == "researcher"


async def test_a_delegation_that_fails_still_ends_and_makes_the_turns_usage_unknown_rather_than_smaller():
    """The failed run spent model calls that no state hands back: reporting the rest as the total would understate what the turn used."""
    record = SubagentRecord("researcher", "Looks things up.", "You look things up.", ("look_up",), None, ("ecorp",), Path("researcher/AGENT.md"))

    @tool
    async def run_subagent(task: str, config: RunnableConfig) -> str:
        """Hand a task to a specialist."""

        def explode(messages):
            raise RuntimeError("the model went away")

        result = await run_subagent_impl(
            "researcher", task, config, domain="shelf", registry={"researcher": (record, ("look_up",))}, tools_by_name={"look_up": look_up}, llm=scripted_llm(explode)
        )
        return result.answer

    # The parent's own calls DO report usage, so only the lost run can make the totals unknown.
    reported = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110}
    asks = AIMessage(content="", tool_calls=[{"name": "run_subagent", "args": {"task": "tea"}, "id": "outer-1"}], usage_metadata=reported)
    graph = _graph(asks, AIMessage(content=ANSWER, usage_metadata=reported), tools=(run_subagent, look_up), display={})

    events = await _events(graph)

    assert _by_call(events, "tool_end")["outer-1"]["outcome"] == "error"
    (usage,) = [event for event in events if event["type"] == "usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["cost_usd"]) == (None, None, None)
    assert usage["model_calls"] == 2, "the parent's two calls are counted; the lost one is not guessed at"
    assert events[-1] == {"type": "done"}
