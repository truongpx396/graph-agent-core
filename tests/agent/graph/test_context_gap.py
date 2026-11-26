"""An answer that says the pre-fetched block does not have it is asked again WITHOUT the block (docs/GRAPH_PATTERNS.md pattern 105).

Real bug: "Who works on Ecorp's engineering team?" got "The retrieved document does not provide specific information..." with `query_employees` bound.
The block was a name match, and with earlier turns in the thread it steered qwen2.5:7b to prose; the same history without the block calls the tool.
The sentences below are the ones the model wrote.
"""
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph.agent_node import NO_BLOCK_REMINDER
from app.agent.graph.build import GraphDeps
from app.agent.graph.messages import (
    CONTEXT_GAP_KEY,
    GATEWAY_REFUSAL_KEY,
    RETRY_ARTIFACT_KEY,
)
from app.agent.graph.output_guardrails import admits_context_gap
from app.agent.graph.retry import graph_node_retry_output
from app.agent.graph.routing import graph_node_check_output
from app.agent.graph.state import MAX_ITERATIONS
from app.core import metrics
from tests import ecorp
from tests.agent.fakes import queue_llm
from tests.conftest import TEST_CTX, metric_value

SOURCES = [
    {"marker": "[1]", "doc_id": "d1", "title": "Support", "text": "Ecorp's support hours are 9am to 5pm on weekdays, and support is free for all open-source users.", "score": -3.7},
    {"marker": "[2]", "doc_id": "d2", "title": "About", "text": "Ecorp was founded in 2021 and builds offline developer tools.", "score": 0.6},
]
GAP = "The retrieved document does not provide specific information about who works on Ecorp's engineering team [1]."


@pytest.mark.parametrize(
    "sentence",
    [
        GAP,
        "The retrieved content does not provide information about Ecorp's parental leave policy.",
        "The retrieval did not provide specific information about who works on Ecorp's engineering team.",
        "The provided information does not include details on how to get a refund.",
        "Ecorp's support documents do not provide specific instructions for resetting passwords.",
        "I can't find information on how to reset your password in our support documents.",
        "I don't have specific information about the response time for gold support in our knowledge base.",
        "The retrieved document doesn’t mention any engineers.",
        "There is no information about parental leave in the retrieved documents.",
    ],
)
def test_an_admission_that_the_documents_lack_it_is_recognised(sentence):
    assert admits_context_gap(sentence)


@pytest.mark.parametrize(
    "sentence",
    [
        "Ecorp's support hours are 9am to 5pm on weekdays [1].",
        "15% of 2,450 is 367.5.",
        "The invoice does not include tax.",  # not about the documents the model was given
        "I can't find that ticket.",  # a tool's result, not the block
        "No ticket #4471 found.",
        "The retrieved document explains payload filtering.",
    ],
)
def test_an_ordinary_answer_is_not(sentence):
    assert not admits_context_gap(sentence)


def _check(answer, citations=SOURCES, before=()):
    messages = [HumanMessage(content="who works in engineering?"), *before, AIMessage(content=answer, id="m1")]
    return ecorp.graph_node_check_output({"messages": messages, "citations": list(citations)})


class TestWhenTheCheckFires:
    def test_the_admission_with_its_stray_marker_dropped_fires(self):
        result = _check(GAP)
        assert result["last_retry_reason"] == "context_gap"
        assert "[1]" not in result["messages"][-1].content  # repaired first (pattern 104), so it is not read as a source in use

    def test_an_answer_with_no_pre_fetched_block_is_left_alone(self):
        assert _check(GAP, citations=[])["last_retry_reason"] is None

    def test_an_answer_after_a_tool_ran_this_turn_is_left_alone(self):
        """The model did look; what it says then is its account of the result."""
        called = AIMessage(content="", tool_calls=[{"name": "search_docs", "args": {"query": "engineers"}, "id": "c1"}])
        result = _check(GAP, before=(called, ToolMessage(content="No relevant documents found.", tool_call_id="c1")))
        assert result["last_retry_reason"] is None

    def test_a_partial_answer_that_uses_a_source_is_kept(self):
        answer = "The retrieved documents do not mention weekend cover. Ecorp's support hours are 9am to 5pm on weekdays [1]."
        assert _check(answer)["last_retry_reason"] is None

    def test_an_admission_that_is_not_the_opening_sentence_is_left_alone(self):
        answer = "Ecorp builds offline developer tools and was founded in 2021 [2]. The retrieved documents do not mention its engineers."
        assert _check(answer)["last_retry_reason"] is None


def test_retry_output_takes_the_answer_and_the_block_back_and_says_nothing_to_the_model():
    update = graph_node_retry_output({"messages": [AIMessage(content=GAP, id="m1")], "last_retry_reason": "context_gap"})
    assert update["context"] == "" and update["citations"] == []
    (taken_back,) = update["messages"]  # no feedback message beside it
    assert (taken_back.id, taken_back.content) == ("m1", GAP)  # same id: it replaces the answer in the thread
    assert taken_back.additional_kwargs[RETRY_ARTIFACT_KEY] and taken_back.additional_kwargs[CONTEXT_GAP_KEY]


TAKEN_BACK = AIMessage(content=GAP, id="a1", additional_kwargs={RETRY_ARTIFACT_KEY: True, CONTEXT_GAP_KEY: True})


def _second_look(answer, *between, **kwargs):
    messages = [HumanMessage(content="who works in engineering?"), TAKEN_BACK, *between, AIMessage(content=answer, id="a2", **kwargs)]
    return ecorp.graph_node_check_output({"messages": messages, "citations": []})


class TestTheSecondLook:
    LOOKED = (
        AIMessage(content="", tool_calls=[{"name": "query_employees", "args": {}, "id": "c1"}]),
        ToolMessage(content="Priya Nair, Staff Engineer", tool_call_id="c1"),
    )

    def test_an_answer_that_looked_nothing_up_is_swapped_for_the_first_one_without_its_marker(self):
        """Measured: this is where "The CEO of Ecorp is John Doe" and generic steps for a new laptop came from."""
        before = metric_value(metrics.agent_context_gap_reask_total, outcome="restored")
        result = _second_look("To request a laptop you would typically submit a ticket [2].")
        assert result["messages"][-1].content == GAP.replace(" [1]", "")
        assert result["messages"][-1].id == "a2"
        assert (result["last_retry_reason"], result["used_citations"], result["ungrounded_claims_count"]) == (None, [], 0)
        assert metric_value(metrics.agent_context_gap_reask_total, outcome="restored") == before + 1

    def test_an_answer_that_looked_something_up_is_kept_and_shows_no_marker(self):
        before = metric_value(metrics.agent_context_gap_reask_total, outcome="looked_up")
        result = _second_look("Priya Nair works in engineering [1].", *self.LOOKED)
        assert result["messages"][-1].content == "Priya Nair works in engineering."
        assert (result["last_retry_reason"], result["ungrounded_claims_count"]) == (None, 0)
        assert metric_value(metrics.agent_context_gap_reask_total, outcome="looked_up") == before + 1

    def test_the_other_checks_still_judge_an_answer_that_looked_something_up(self):
        assert _second_look(ecorp.ECORP_SYSTEM_PROMPT[:200], *self.LOOKED)["last_retry_reason"] == "leaked_prompt"

    def test_a_gateway_refusal_is_never_swapped_back(self):
        result = _second_look("I can't help with that request.", additional_kwargs={GATEWAY_REFUSAL_KEY: True})
        assert "messages" not in result


def test_switched_off_the_check_does_not_run():
    state = {"messages": [HumanMessage(content="who?"), AIMessage(content=GAP, id="m1")], "citations": list(SOURCES)}
    assert graph_node_check_output(state, system_prompt=ecorp.ECORP_SYSTEM_PROMPT, checks_off=frozenset({"context_gap"}))["last_retry_reason"] is None


class TestEndToEnd:
    QUESTION = "Who works on Ecorp's engineering team?"

    async def _run(self, *replies):
        async def search(query, ctx):
            return "\n".join(f"{s['marker']} {s['text']}" for s in SOURCES), list(SOURCES)

        seen: list = []
        # One spare reply: the follow-up suggestions make their own call on the same fake.
        graph = ecorp.build_graph(GraphDeps(llm=queue_llm(*replies, AIMessage(content="[]"), seen=seen), search_docs=search))
        config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}, "recursion_limit": MAX_ITERATIONS * 2 + 15}
        result = await graph.ainvoke({"messages": [HumanMessage(content=self.QUESTION)]}, config=config)
        return result, seen

    async def test_the_second_call_drops_the_block_so_the_model_can_use_a_tool(self):
        calculate = AIMessage(content="", tool_calls=[{"name": "calculator", "args": {"expression": "2+2"}, "id": "c1"}])  # stands in for query_employees: hermetic
        before = metric_value(metrics.agent_retry_total)
        result, seen = await self._run(AIMessage(content=GAP), calculate, AIMessage(content="Four engineers work there."))

        first, second = seen[0], seen[1]
        assert any("<retrieved_document>" in str(m.content) for m in first)
        sent_again = "\n".join(str(m.content) for m in second)
        assert "<retrieved_document>" not in sent_again.split("system message or a request", 1)[-1]  # the system prompt names the tag; nothing after it carries one
        assert "does not provide specific information" not in sent_again  # neither the rejected answer nor feedback about it
        asked = f"{self.QUESTION}\n\n{NO_BLOCK_REMINDER}"
        assert [str(m.content) for m in second if isinstance(m, HumanMessage)] == [asked]
        # The line stays through the tool round of the re-ask, and is not stored in the thread.
        assert [str(m.content) for m in seen[2] if isinstance(m, HumanMessage)] == [asked]
        assert [str(m.content) for m in result["messages"] if isinstance(m, HumanMessage)] == [self.QUESTION]
        assert result["messages"][-1].content == "Four engineers work there."
        # The first answer stays in the thread only as an artifact: no transcript shows it and no later turn is sent it.
        kept = [m for m in result["messages"] if "does not provide specific information" in str(m.content)]
        assert [m.additional_kwargs.get(CONTEXT_GAP_KEY) for m in kept] == [True]
        assert metric_value(metrics.agent_retry_total) == before + 1

    async def test_it_happens_once_and_a_second_look_with_no_tool_ends_on_the_first_answer(self):
        result, _ = await self._run(AIMessage(content=GAP), AIMessage(content="You would typically ask your manager."))
        assert result["messages"][-1].content == GAP.replace(" [1]", "")
        assert result["iterations"] == 2

    async def test_the_next_turn_is_not_sent_the_answer_that_was_taken_back(self):
        async def search(query, ctx):
            return "", []

        seen: list = []
        graph = ecorp.build_graph(GraphDeps(llm=queue_llm(AIMessage(content="Fine, thanks."), AIMessage(content="[]"), seen=seen), search_docs=search))
        config = {"configurable": {"thread_id": str(uuid.uuid4()), "ctx": TEST_CTX}, "recursion_limit": MAX_ITERATIONS * 2 + 15}
        earlier = [HumanMessage(content="who works in engineering?"), TAKEN_BACK, AIMessage(content="Priya Nair does.")]
        await graph.ainvoke({"messages": [*earlier, HumanMessage(content="and how are you?")]}, config=config)
        assert "does not provide specific information" not in "\n".join(str(m.content) for m in seen[0])
