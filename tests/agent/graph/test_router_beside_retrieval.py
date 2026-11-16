"""The model router runs beside retrieval, and a domain may leave retrieval out (PRODUCTION_REVIEW.md R29).

`route_model` runs before `retrieve_context`, but the two do not depend on each other: the router reads only the person's message and writes only
`model_route`; retrieval writes only `context`, `citations` and `context_anchor_index`. Run one after the other, the router's latency (up to
ROUTER_TIMEOUT_SECONDS) was added to every routed turn before retrieval began. `agent` now waits for both (a barrier), and `AgentManifest.prefetch`
lets a domain whose tools do not read documents leave `retrieve_context` out of its graph.
"""
import asyncio
from dataclasses import replace

from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import model_route
from app.agent.graph.build import GraphDeps, GraphProfile
from app.ports.outbound.classification import (
    NO_ANSWER as NO_LABEL,  # a label the router can do nothing with: the rules decide
)
from tests.agent.fakes import cfg, scripted_llm
from tests.ecorp import ECORP_MANIFEST, build_graph, build_subagent_graph
from tests.fakes.ml import use_classifier

WAIT = 3


def _edges(graph):
    return {(e.source, e.target) for e in graph.get_graph().edges}


def _llm():
    return scripted_llm(lambda _m: AIMessage(content="The support desk is open nine to five on weekdays."))


async def _miss(ctx, query):
    return None


async def _noop_set(*args):
    return None


def _router_on(monkeypatch, choose):
    monkeypatch.setattr(model_route, "ROUTER_MODE", "on")
    monkeypatch.setattr(model_route, "STRONG_CHAT_MODEL", "chat-strong")
    monkeypatch.setattr(model_route, "ROUTER_MAX_TIER", "strong")
    use_classifier(monkeypatch, choose)


async def test_the_router_and_retrieval_run_in_the_same_step(monkeypatch):
    """Each waits for the other to have STARTED: run one after the other, the first would sit out its whole wait and fail."""
    router_started, retrieval_started = asyncio.Event(), asyncio.Event()
    met = []

    async def choose(question, text):
        router_started.set()
        met.append(await _started(retrieval_started))
        return NO_LABEL

    async def search(query, ctx):
        retrieval_started.set()
        met.append(await _started(router_started))
        return "", []

    _router_on(monkeypatch, choose)
    graph = build_graph(GraphDeps(llm=_llm(), search_docs=search, cache_get=_miss, cache_set=_noop_set))

    await graph.ainvoke({"messages": [HumanMessage(content="what are the support hours")]}, config=cfg())

    assert met == [True, True], "one of them finished before the other began"


async def _started(event):
    try:
        await asyncio.wait_for(event.wait(), WAIT)
        return True
    except TimeoutError:
        return False




async def test_agent_waits_for_the_slower_of_the_two_because_the_model_call_reads_model_route(monkeypatch):
    """A slow router holds `agent` back: it starts only after BOTH have ended."""

    async def choose(question, text):
        await asyncio.sleep(0.2)  # much slower than retrieval
        return NO_LABEL

    async def search(query, ctx):
        return "", []

    _router_on(monkeypatch, choose)
    graph = build_graph(GraphDeps(llm=_llm(), search_docs=search, cache_get=_miss, cache_set=_noop_set))
    order = []

    async for event in graph.astream_events({"messages": [HumanMessage(content="what are the support hours")]}, config=cfg(), version="v2"):
        node = event.get("metadata", {}).get("langgraph_node")
        if node in {"route_model", "retrieve_context", "agent"} and node == event["name"] and event["event"] in {"on_chain_start", "on_chain_end"}:
            order.append((event["event"], node))

    assert order.index(("on_chain_end", "route_model")) < order.index(("on_chain_start", "agent"))
    assert order.index(("on_chain_end", "retrieve_context")) < order.index(("on_chain_start", "agent"))
    assert order.index(("on_chain_end", "retrieve_context")) < order.index(("on_chain_end", "route_model")), "retrieval did not wait for the slow router"


def test_the_wiring_is_one_fan_out_and_one_join():
    edges = _edges(build_graph(GraphDeps(llm=_llm())))

    assert ("route_model", "agent") in edges and ("retrieve_context", "agent") in edges
    assert ("route_model", "retrieve_context") not in edges, "no longer one after the other"


def test_without_a_router_retrieval_goes_straight_to_the_agent_as_before():
    edges = _edges(build_subagent_graph(GraphDeps(llm=_llm())))

    assert ("retrieve_context", "agent") in edges and not {s for s, _ in edges} & {"route_model"}


def test_the_graph_without_cache_or_compaction_still_fans_out_from_moderation():
    edges = _edges(build_graph(GraphDeps(llm=_llm()), profile=GraphProfile(include_cache=False, include_compaction=False, include_followups=False)))

    assert ("moderate_input", "route_model") in edges and ("moderate_input", "retrieve_context") in edges


# --- A domain that does not pre-fetch (the manifest's `prefetch`) -------------------------------------------------------


def _never():
    return replace(ECORP_MANIFEST, prefetch="never")


def test_a_domain_that_never_prefetches_has_no_retrieval_node():
    graph = build_graph(GraphDeps(llm=_llm()), manifest=_never())

    assert "retrieve_context" not in graph.get_graph().nodes
    assert ("route_model", "agent") in _edges(graph)


def test_a_domain_that_never_prefetches_and_has_no_router_goes_from_compaction_straight_to_the_agent():
    graph = build_subagent_graph(GraphDeps(llm=_llm()), manifest=_never())

    assert "retrieve_context" not in graph.get_graph().nodes and "route_model" not in graph.get_graph().nodes
    assert ("moderate_input", "agent") in _edges(graph)


async def test_a_domain_that_never_prefetches_reaches_the_model_without_a_search(monkeypatch):
    searched = []

    async def search(query, ctx):
        searched.append(query)
        return "[1] SOMETHING", [{"marker": "[1]", "text": "SOMETHING"}]

    graph = build_graph(GraphDeps(llm=_llm(), search_docs=search, cache_get=_miss, cache_set=_noop_set), manifest=_never())

    result = await graph.ainvoke({"messages": [HumanMessage(content="what are the support hours")]}, config=cfg())

    assert searched == [], "a pre-fetch ran for a domain that chose never"
    assert "nine to five" in result["messages"][-1].content


async def test_the_default_is_unchanged_every_shipped_domain_still_prefetches():
    from app.domains.ecorp.domain import ECORP_MANIFEST as ecorp
    from app.domains.ops.domain import OPS_MANIFEST
    from app.domains.sales.domain import SALES_MANIFEST
    from app.domains.support.domain import SUPPORT_MANIFEST

    assert [m.prefetch for m in (ecorp, OPS_MANIFEST, SUPPORT_MANIFEST, SALES_MANIFEST)] == ["always"] * 4
