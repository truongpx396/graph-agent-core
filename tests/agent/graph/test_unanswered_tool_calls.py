"""A tool call with no result on record must not leave a conversation unusable: OpenAI-style APIs reject the whole history (GRAPH_PATTERNS.md pattern 16).

LangGraph 0.6 cannot resume an approval paused under 0.2, which leaves exactly that behind. The copy sent to the model closes the call;
nothing runs and the saved thread is left as it was.
"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph.build import GraphDeps
from app.agent.graph.messages import (
    UNRECORDED_CALL_MESSAGE,
    with_unanswered_calls_closed,
)
from tests.agent.fakes import call, cfg, orphan_tool_call_ids, scripted_llm
from tests.ecorp import build_graph

ANSWER = "A sufficiently long final answer."


def test_only_calls_without_a_result_are_closed_right_after_the_message_that_made_them():
    two = AIMessage(content="", tool_calls=[{"name": "calculator", "args": {}, "id": "a"}, {"name": "add_note", "args": {}, "id": "b"}])
    history = [HumanMessage(content="q"), two, ToolMessage(content="42", tool_call_id="a"), HumanMessage(content="next")]

    closed = with_unanswered_calls_closed(history)

    assert [type(m).__name__ for m in closed] == ["HumanMessage", "AIMessage", "ToolMessage", "ToolMessage", "HumanMessage"]
    assert (closed[2].tool_call_id, closed[2].name, closed[2].content) == ("b", "add_note", UNRECORDED_CALL_MESSAGE)
    assert not orphan_tool_call_ids(closed)
    assert with_unanswered_calls_closed(closed) == closed and len(history) == 4, "nothing is added twice, and the input is not changed"


async def test_a_thread_with_a_stranded_call_still_answers_and_the_saved_history_is_not_rewritten():
    seen: list = []
    graph = build_graph(GraphDeps(llm=scripted_llm(lambda _m: AIMessage(content=ANSWER), seen=seen)))
    config = cfg()
    # What an unresumable pause leaves: the call is in the thread, no result is.
    await graph.aupdate_state(
        config, {"messages": [HumanMessage(content="remember this"), call("add_note", {"title": "t"}, "stranded")]}, as_node="write_semantic_cache",
    )

    out = await graph.ainvoke({"messages": [HumanMessage(content="never mind, what is new")]}, config)

    assert out["messages"][-1].content == ANSWER
    assert seen and not orphan_tool_call_ids(seen[-1]), "the model was sent a tool call with no result"
    assert orphan_tool_call_ids(out["messages"]) == ["stranded"], "the saved thread is not rewritten"
    assert not any(isinstance(m, ToolMessage) for m in out["messages"]), "no tool ran"
