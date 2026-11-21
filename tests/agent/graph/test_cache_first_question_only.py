"""The answer cache is consulted and written only for a conversation's FIRST question (PRODUCTION_REVIEW.md R33, pattern 22).

`check_semantic_cache` looked up the person's last message alone, by meaning, among everything that person asked in the last hour in ANY conversation.
"yes", "do it", "more detail please" and "and for March?" mean nothing on their own, so they matched whatever similar short message was cached, and the
cached answer belonged to another conversation. A first question is the only one whose meaning does not depend on the conversation.
"""
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.graph.cache import (
    make_graph_node_check_semantic_cache,
    make_graph_node_write_semantic_cache,
)
from app.agent.graph.messages import COMPACTION_MARKER_KEY, RETRY_ARTIFACT_KEY
from tests.conftest import TEST_CTX

CAPS = {"search_docs": "read_only"}


class _Cache:
    def __init__(self):
        self.hit, self.asked, self.written = ("A cached answer.", []), [], []

    async def get(self, ctx, query):
        self.asked.append(query)
        return self.hit

    async def set(self, ctx, query, answer, citations):
        self.written.append((query, answer))


def _first_question():
    return {"messages": [HumanMessage(content="What is the refund policy?")], "ctx": TEST_CTX}


def _follow_up(text="yes"):
    return {
        "messages": [HumanMessage(content="What is the refund policy?"), AIMessage(content="Thirty days. Want the form?"), HumanMessage(content=text)],
        "ctx": TEST_CTX,
    }


async def test_a_first_question_is_looked_up():
    cache = _Cache()

    result = await make_graph_node_check_semantic_cache(cache.get)(_first_question())

    assert cache.asked == ["What is the refund policy?"] and result["cache_hit"] is True


async def test_a_follow_up_never_calls_the_cache_so_it_cannot_be_answered_from_another_conversation():
    cache = _Cache()

    for text in ("yes", "do it", "more detail please", "and for March?"):
        assert await make_graph_node_check_semantic_cache(cache.get)(_follow_up(text)) == {}

    assert cache.asked == []


async def test_a_follow_up_is_never_stored_and_a_first_question_still_is():
    cache = _Cache()
    write = make_graph_node_write_semantic_cache(cache.set, CAPS)

    assert await write({**_follow_up(), "messages": _follow_up()["messages"] + [AIMessage(content="Here is the form.")]}) == {}
    assert cache.written == []

    await write({"messages": _first_question()["messages"] + [AIMessage(content="Thirty days.")], "ctx": TEST_CTX, "used_citations": []})

    assert cache.written == [("What is the refund policy?", "Thirty days.")]


async def test_retry_feedback_is_not_an_earlier_question():
    """`retry_output`'s feedback is a HumanMessage tagged as an artifact: it is the graph's, not the person's."""
    cache = _Cache()
    state = {
        "messages": [
            HumanMessage(content="What is the refund policy?"),
            AIMessage(content="Too short.", additional_kwargs={RETRY_ARTIFACT_KEY: True}),
            HumanMessage(content="Please expand.", additional_kwargs={RETRY_ARTIFACT_KEY: True}),
        ],
        "ctx": TEST_CTX,
    }

    await make_graph_node_check_semantic_cache(cache.get)(state)

    assert cache.asked == ["What is the refund policy?"]


async def test_a_thread_whose_earlier_turns_were_compacted_away_is_not_at_its_start():
    """`compact_history` replaces older turns with a breadcrumb, so no earlier human message may be left in `messages`."""
    cache = _Cache()
    state = {
        "messages": [
            SystemMessage(content="[3 earlier turns summarized.]", additional_kwargs={COMPACTION_MARKER_KEY: True}),
            HumanMessage(content="yes"),
        ],
        "ctx": TEST_CTX,
    }

    assert await make_graph_node_check_semantic_cache(cache.get)(state) == {}
    assert cache.asked == []


async def test_tool_messages_between_the_turns_do_not_make_a_follow_up_look_first():
    cache = _Cache()
    state = {
        "messages": [
            HumanMessage(content="look it up"),
            AIMessage(content="", tool_calls=[{"name": "search_docs", "args": {}, "id": "c1"}]),
            ToolMessage(content="found", tool_call_id="c1"),
            AIMessage(content="Found it."),
            HumanMessage(content="and the other one?"),
        ],
        "ctx": TEST_CTX,
    }

    assert await make_graph_node_check_semantic_cache(cache.get)(state) == {}


async def test_with_the_setting_off_neither_node_touches_the_cache_even_for_a_first_question():
    cache = _Cache()
    check = make_graph_node_check_semantic_cache(cache.get, enabled=False)
    write = make_graph_node_write_semantic_cache(cache.set, CAPS, enabled=False)

    assert await check(_first_question()) == {}
    assert await write({"messages": _first_question()["messages"] + [AIMessage(content="Thirty days.")], "ctx": TEST_CTX, "used_citations": []}) == {}

    assert cache.asked == [] and cache.written == []


async def test_the_nodes_stay_in_the_graph_whatever_the_setting(monkeypatch):
    """A paused conversation's checkpoint names nodes: a setting must not change the topology."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

    from app.agent.graph import build as graph_build
    from app.agent.graph.build import GraphDeps
    from tests.ecorp import build_graph

    monkeypatch.setattr(graph_build, "SEMANTIC_CACHE_ENABLED", False)

    graph = build_graph(GraphDeps(llm=GenericFakeChatModel(messages=iter([]))))

    assert {"check_semantic_cache", "write_semantic_cache"} <= set(graph.get_graph().nodes)
