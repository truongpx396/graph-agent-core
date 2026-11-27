"""Each conditional edge is registered under a fixed name (`build.EDGE_CHANNEL_NAMES`, through `named_edge`), not its router's Python name.

Under LangGraph 0.2 that name was part of what a checkpoint stored, so changing it stranded a turn left mid-flight. Since 0.3 a checkpoint
stores the TARGET node (`branch:to:<node>`) and converts older rows on read, so the names no longer decide what a resume runs
(`test_checkpoint_across_a_topology_change.py`); they still label the edges, and the table stays complete and stable.
"""
import dataclasses

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel

from app.agent.graph import build
from app.agent.graph.build import GraphDeps
from app.agent.graph.utils import named_edge
from tests.ecorp import (
    ECORP_DOMAIN_PLUGIN,
    ECORP_MANIFEST,
    build_graph,
    build_subagent_graph,
)

STORED = {
    "validate_input": "route_after_validation",
    "moderate_input": "route",
    "check_semantic_cache": "route",
    "compact_history": "route",
    "agent": "condition",
    "human_approval": "route_after_approval",
    "check_output": "route_after_check_bound",
}


def _deps() -> GraphDeps:
    return GraphDeps(llm=GenericFakeChatModel(messages=iter([])))


def _graphs():
    no_prefetch = dataclasses.replace(ECORP_MANIFEST, prefetch="never")
    return {
        "the conversation": build_graph(_deps()),
        "a nested subagent run": build_subagent_graph(_deps()),
        "a domain without pre-fetch": build_graph(_deps(), manifest=no_prefetch, domain=ECORP_DOMAIN_PLUGIN),
    }


def test_the_table_holds_the_registered_edge_names():
    assert dict(build.EDGE_CHANNEL_NAMES) == STORED


@pytest.mark.parametrize("graph_name", ["the conversation", "a nested subagent run", "a domain without pre-fetch"])
def test_each_conditional_edge_has_its_table_entry_in_every_graph_shape(graph_name):
    graph = _graphs()[graph_name]
    stored = {source: set(by_name) for source, by_name in graph.builder.branches.items()}
    assert stored, "the graph has no conditional edges, so there is nothing to check"
    assert stored == {source: {STORED[source]} for source in stored}
    # What a checkpoint stores: a channel per target node, so a node's name is what must not move.
    targets = {c.removeprefix("branch:to:") for c in graph.channels if c.startswith("branch:to:")}
    assert targets and targets <= set(graph.nodes), "every stored trigger channel must name a node of this graph"


def test_the_conversation_has_a_conditional_edge_after_every_node_in_the_table():
    assert set(_graphs()["the conversation"].builder.branches) == set(STORED)


def test_pinning_a_router_keeps_the_targets_it_declares_for_the_diagram():
    """`functools.wraps` carries a `Literal[...]` return annotation across; without it LangGraph would draw the edge to every node."""
    edge = _graphs()["the conversation"].builder.branches["validate_input"]["route_after_validation"]
    assert set(edge.ends) == {"moderate_input", "reject_context", "reject_input"}


def test_named_edge_changes_only_the_name_not_the_answer():
    def graph_edge_route_after_somewhere(state):
        return state["go"]

    pinned = named_edge(graph_edge_route_after_somewhere, "route_after_before_the_rename")
    assert pinned.__name__ == "route_after_before_the_rename"
    assert pinned({"go": "next_node"}) == "next_node"
    assert graph_edge_route_after_somewhere.__name__ == "graph_edge_route_after_somewhere"
