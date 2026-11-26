"""`graph/stream_events.py`: nodes announce what the client should see (PRODUCTION_REVIEW.md R35, part A).

The runtime used to match `on_chain_end` events for four node names and read their outputs. Renaming a node, or adding one that also replaces the answer,
silently changed what the client saw. These test the wrapper and, through a real graph, that the four nodes announce what they used to be recognised for.
"""
import uuid

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph.build import GraphDeps
from app.agent.graph.stream_events import (
    STREAM_EVENT,
    announcing,
    judged_the_answer,
    rejected_the_answer,
    replaced_the_answer,
    trimmed_the_history,
)
from tests.agent.fakes import cfg
from tests.ecorp import build_graph


class TestTheAnnouncementsEachNodeMakes:
    def test_a_replaced_answer_is_a_retry_then_the_new_text(self):
        update = {"messages": [AIMessage(content="The corrected answer [1].")]}

        assert replaced_the_answer(update) == [{"type": "retry"}, {"type": "token", "content": "The corrected answer [1]."}]

    def test_an_update_that_left_the_answer_alone_announces_nothing(self):
        assert replaced_the_answer({}) == [] and replaced_the_answer({"messages": []}) == []

    def test_a_replacement_with_no_text_is_a_retry_alone(self):
        assert replaced_the_answer({"messages": [AIMessage(content="")]}) == [{"type": "retry"}]

    def test_a_rejected_answer_is_always_a_retry(self):
        assert rejected_the_answer({}) == [{"type": "retry"}]

    def test_compaction_announces_only_when_it_trimmed_something(self):
        assert trimmed_the_history({"messages": [object()]}) == [{"type": "compacted"}]
        assert trimmed_the_history({}) == []


class TestTheWrapper:
    async def test_it_returns_the_nodes_update_unchanged_and_announces_after_running_it(self):
        sent = []

        async def dispatch(name, event, config=None):
            sent.append((name, event))

        import app.agent.graph.stream_events as module

        original = module.adispatch_custom_event
        module.adispatch_custom_event = dispatch
        try:
            node = announcing(lambda state: {"messages": [AIMessage(content="x")]}, replaced_the_answer)
            update = await node({}, {"configurable": {}})
        finally:
            module.adispatch_custom_event = original

        assert update == {"messages": [update["messages"][0]]}
        assert sent == [(STREAM_EVENT, {"type": "retry"}), (STREAM_EVENT, {"type": "token", "content": "x"})]

    async def test_called_with_no_config_it_dispatches_nothing_and_still_returns_the_update(self):
        """A direct call in a unit test: there is no run to announce to."""
        node = announcing(lambda state: {"ok": True}, rejected_the_answer)

        assert await node({}) == {"ok": True}

    async def test_an_async_node_is_awaited(self):
        async def inner(state):
            return {"awaited": True}

        assert await announcing(inner, lambda update: [])({}) == {"awaited": True}

    async def test_config_is_passed_on_to_a_node_that_takes_one_and_withheld_from_one_that_does_not(self):
        seen = {}

        def takes(state, config=None):
            seen["config"] = config
            return {}

        def does_not(state):
            return {}

        await announcing(takes, lambda u: [])({}, {"marker": 1})
        await announcing(does_not, lambda u: [])({}, {"marker": 1})

        assert seen["config"] == {"marker": 1}

    def test_langgraph_sees_a_config_parameter_on_the_wrapper_not_the_wrapped_nodes_signature(self):
        """The reason it is not `functools.wraps`: LangGraph passes `config` only to a node whose signature has one."""
        import inspect

        wrapped = announcing(lambda state: {}, rejected_the_answer)

        assert "config" in inspect.signature(wrapped).parameters


async def _announcements(graph, text):
    seen = []
    async for event in graph.astream_events({"messages": [HumanMessage(content=text)]}, config=cfg(str(uuid.uuid4())), version="v2"):
        if event["event"] == "on_custom_event" and event["name"] == STREAM_EVENT:
            seen.append((event["metadata"].get("langgraph_node"), event["data"]))
    return seen


async def test_a_rejected_answer_through_the_real_graph_is_announced_by_retry_output_before_the_next_answer():
    """Two answers: a deferral the check rejects, then a good one. The client is told to discard the first."""
    llm = GenericFakeChatModel(messages=iter([
        AIMessage(content="I will use the search_docs tool to look up the refund policy for you."),
        AIMessage(content="Refunds are possible within thirty days of purchase, with a receipt."),
    ]))

    seen = await _announcements(build_graph(GraphDeps(llm=llm)), "what is the refund policy?")

    assert ("retry_output", {"type": "retry"}) in seen


async def test_a_turn_nothing_corrected_only_releases_which_no_client_sees():
    llm = GenericFakeChatModel(messages=iter([AIMessage(content="Refunds are possible within thirty days of purchase, with a receipt.")]))

    assert await _announcements(build_graph(GraphDeps(llm=llm)), "what is the refund policy?") == [("check_output", {"type": "release"})]


async def test_a_nested_subagent_run_announces_with_its_own_tag_so_the_parent_can_tell_it_apart(monkeypatch):
    """The subagent's graph has the same nodes; its announcements must carry `subagent_name`, or the parent would act on them."""
    from tests.agent.fakes import queue_llm

    graph = build_graph(GraphDeps(llm=queue_llm(AIMessage(content="I will use the search_docs tool to look up the refund policy for you."), AIMessage(content="Refunds take thirty days from purchase."))))
    config = cfg(str(uuid.uuid4()))
    config["metadata"] = {"subagent_name": "researcher"}  # how run_subagent_impl tags the nested run's config

    seen = []
    async for event in graph.astream_events({"messages": [HumanMessage(content="refund policy?")]}, config=config, version="v2"):
        if event["event"] == "on_custom_event" and event["name"] == STREAM_EVENT:
            seen.append(event["metadata"].get("subagent_name"))

    assert seen and set(seen) == {"researcher"}


class TestWhatCheckOutputAnnounces:
    def test_an_answer_that_stands_releases_whatever_was_held(self):
        assert judged_the_answer({"last_retry_reason": None}) == [{"type": "release"}]

    def test_a_repaired_answer_is_sent_as_the_replacement(self):
        update = {"messages": [AIMessage(content="Fixed.")], "last_retry_reason": None}
        assert judged_the_answer(update) == [{"type": "retry"}, {"type": "token", "content": "Fixed."}]

    def test_nothing_is_sent_for_an_answer_that_is_about_to_be_taken_back_even_if_it_was_repaired(self):
        assert judged_the_answer({"messages": [AIMessage(content="Not found.")], "last_retry_reason": "context_gap"}) == []

    def test_another_rejection_releases_nothing_the_retry_node_announces_it(self):
        assert judged_the_answer({"last_retry_reason": "too_short"}) == []
