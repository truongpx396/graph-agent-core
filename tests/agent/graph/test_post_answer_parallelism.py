"""Once an answer stands, the follow-up suggestions and the semantic-cache write run in parallel.

They never depended on each other (the cache write reads the answer and its citations; the follow-ups
only add `followups`), but were wired one after the other, so `done` waited for the follow-up LLM
call and THEN the cache write's embedding and Redis round trip.
"""
import threading

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END

from app.agent.graph.build import GraphDeps
from tests.agent.fakes import cfg, scripted_llm
from tests.ecorp import build_graph, build_subagent_graph

WAIT = 3  # seconds a step waits for the other before the test gives up on it overlapping


def _edges(graph):
    return {(e.source, e.target) for e in graph.get_graph().edges}


def test_neither_step_comes_before_the_other():
    edges = _edges(build_graph(GraphDeps(llm=scripted_llm(lambda _m: AIMessage(content="x")))))
    assert ("check_output", "suggest_followups") in edges
    assert ("check_output", "write_semantic_cache") in edges
    assert ("suggest_followups", "__end__") in edges and ("write_semantic_cache", "__end__") in edges
    assert ("suggest_followups", "write_semantic_cache") not in edges


def test_a_graph_without_the_optional_steps_ends_right_after_check_output():
    # Read from the builder: LangGraph 0.6's drawing stops at a router that declares no targets (`agent`'s), so it never reaches this node's edges.
    graph = build_subagent_graph(GraphDeps(llm=scripted_llm(lambda _m: AIMessage(content="x"))))
    (after_check,) = graph.builder.branches["check_output"].values()
    assert END in after_check.ends.values()
    assert not {"suggest_followups", "write_semantic_cache"} & set(graph.nodes)


async def test_the_cache_write_begins_while_the_follow_ups_are_still_being_generated():
    followups_in_flight, cache_started, followups_done = (threading.Event() for _ in range(3))
    overlapped: list[bool] = []

    def respond(messages):
        if str(messages[-1].content).startswith("Based on the answer below"):  # the follow-ups call
            followups_in_flight.set()
            cache_started.wait(WAIT)  # held here (in a worker thread) until the cache write begins
            followups_done.set()
            return AIMessage(content="What else?\nAnd more?")
        return AIMessage(content="The refund policy text says what it says [1].")

    async def search(query, ctx):
        return "[1] REFUND POLICY TEXT", [{"marker": "[1]", "text": "REFUND POLICY TEXT"}]

    async def miss(ctx, query):
        return None

    async def cache_set(ctx, query, answer, citations):
        import asyncio

        started_after_followups = await asyncio.to_thread(followups_in_flight.wait, WAIT)
        overlapped.append(started_after_followups and not followups_done.is_set())
        cache_started.set()

    g = build_graph(
        GraphDeps(llm=scripted_llm(respond), search_docs=search, cache_get=miss, cache_set=cache_set)
    )
    result = await g.ainvoke({"messages": [HumanMessage(content="what is the refund policy")]}, config=cfg())

    assert overlapped == [True], "the cache write waited for the follow-ups to finish"
    assert result["followups"] == ["What else?", "And more?"]


# --- A domain that does not want follow-ups pays for none (PRODUCTION_REVIEW.md R30) -------------------------------------


def _cited_answer_calls(graph_kwargs, monkeypatch=None):
    calls = []

    def respond(messages):
        calls.append(str(messages[-1].content)[:30])
        if str(messages[-1].content).startswith("Based on the answer below"):
            return AIMessage(content="What else?\nAnd more?")
        return AIMessage(content="The refund policy text says what it says [1].")

    async def search(query, ctx):
        return "[1] REFUND POLICY TEXT", [{"marker": "[1]", "text": "REFUND POLICY TEXT"}]

    async def miss(ctx, query):
        return None

    async def cache_set(ctx, query, answer, citations):
        return None

    graph = build_graph(GraphDeps(llm=scripted_llm(respond), search_docs=search, cache_get=miss, cache_set=cache_set), **graph_kwargs)
    return graph, calls


async def test_a_domain_without_followups_makes_one_model_call_for_a_cited_answer():
    from dataclasses import replace

    from tests.ecorp import ECORP_MANIFEST

    graph, calls = _cited_answer_calls({"manifest": replace(ECORP_MANIFEST, suggest_followups=False)})

    result = await graph.ainvoke({"messages": [HumanMessage(content="what is the refund policy")]}, config=cfg())

    assert len(calls) == 1, "the follow-ups call was made anyway"
    assert not result.get("followups")
    assert "suggest_followups" not in graph.get_graph().nodes
    assert ("check_output", "write_semantic_cache") in _edges(graph)


async def test_a_domain_that_keeps_followups_still_makes_the_second_call():
    graph, calls = _cited_answer_calls({})

    result = await graph.ainvoke({"messages": [HumanMessage(content="what is the refund policy")]}, config=cfg())

    assert len(calls) == 2 and result["followups"] == ["What else?", "And more?"]


async def test_the_operators_setting_turns_it_off_for_every_domain_at_build_time(monkeypatch):
    from app.agent.graph import build as graph_build

    monkeypatch.setattr(graph_build, "FOLLOWUPS_ENABLED", False)
    graph, calls = _cited_answer_calls({})

    await graph.ainvoke({"messages": [HumanMessage(content="what is the refund policy")]}, config=cfg())

    assert len(calls) == 1 and "suggest_followups" not in graph.get_graph().nodes


def test_the_shipped_defaults_keep_it_for_ecorp_where_the_page_shows_the_chips_and_drop_it_where_answers_end_in_an_action():
    from app.domains.ecorp.domain import ECORP_MANIFEST
    from app.domains.ops.domain import OPS_MANIFEST
    from app.domains.sales.domain import SALES_MANIFEST
    from app.domains.support.domain import SUPPORT_MANIFEST

    assert (ECORP_MANIFEST.suggest_followups, OPS_MANIFEST.suggest_followups, SUPPORT_MANIFEST.suggest_followups, SALES_MANIFEST.suggest_followups) == (
        True, False, False, False,
    )
