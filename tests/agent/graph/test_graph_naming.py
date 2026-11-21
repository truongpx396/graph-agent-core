"""The graph's naming rule (GRAPH_PATTERNS.md "Naming"), held by reading `build_graph`'s own wiring:

- the function behind the node registered as `"<node>"` is `graph_node_<node>`, or built by `make_graph_node_<node>` (optionally `_bound`);
- the router of `<node>`'s conditional edge is `graph_edge_route_after_<node>` (optionally `_bound`), where `<node>` is the SOURCE node;
- a factory returns a function named `graph_node_<node>`, and the graph modules define no router or node under another scheme.

The rule is read from the source, not from LangGraph, because what it protects is a reader's ability to go from a registered name to its code.
"""
import ast
import re
from pathlib import Path

import pytest

from app.agent.graph import build

GRAPH_DIR = Path(build.__file__).parent
BUILD_TREE = ast.parse(Path(build.__file__).read_text(encoding="utf-8"))


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}


def _registrations(helper: str) -> list[tuple[str, set[str]]]:
    """(the node's name, every identifier in the expression registered for it) for each `helper("<node>", <expr>, ...)` in build.py."""
    found = []
    for call in ast.walk(BUILD_TREE):
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == helper and len(call.args) >= 2:
            first = call.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                found.append((first.value, _names_in(call.args[1])))
    return found


def _module_functions() -> dict[str, list[str]]:
    return {
        path.name: [n.name for n in ast.parse(path.read_text(encoding="utf-8")).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        for path in sorted(GRAPH_DIR.glob("*.py"))
    }


NODES = _registrations("add")
EDGES = _registrations("add_edges_after")


def test_the_builder_registers_the_nodes_and_edges_this_test_reads():
    """If the helpers are renamed or the wiring moves, the parametrised checks below would pass over nothing."""
    assert len(NODES) >= 15
    assert {"validate_input", "agent", "tools", "human_approval", "check_output"} <= {n for n, _ in NODES}
    assert {"validate_input", "agent", "human_approval", "check_output"} <= {n for n, _ in EDGES}


@pytest.mark.parametrize(("node", "names"), NODES, ids=[n for n, _ in NODES])
def test_a_registered_node_is_implemented_by_graph_node_followed_by_its_name(node, names):
    expected = re.compile(rf"^(make_)?graph_node_{node}(_bound)?$")
    assert any(expected.match(n) for n in names), (
        f"node {node!r} is registered from {sorted(names)}: none is `graph_node_{node}`, `make_graph_node_{node}` or the `_bound` form"
    )


@pytest.mark.parametrize(("source", "names"), EDGES, ids=[n for n, _ in EDGES])
def test_a_conditional_edge_is_routed_by_graph_edge_route_after_its_source_node(source, names):
    expected = re.compile(rf"^graph_edge_route_after_{source}(_bound)?$")
    assert any(expected.match(n) for n in names), (
        f"the edge after {source!r} is routed by {sorted(names)}: none is `graph_edge_route_after_{source}` or its `_bound` form"
    )


def test_every_graph_node_and_router_a_module_defines_belongs_to_a_registration():
    """A leftover `graph_node_x` or `graph_edge_route_after_x` that nothing registers is a node or edge that was removed, or a typo."""
    nodes = {n for n, _ in NODES}
    sources = {n for n, _ in EDGES}
    for module, functions in _module_functions().items():
        for fn in functions:
            if m := re.fullmatch(r"(?:make_)?graph_node_(\w+)", fn):
                assert m.group(1) in nodes, f"{module}::{fn} is not registered as a node in build.py"
            if m := re.fullmatch(r"graph_edge_route_after_(\w+)", fn):
                assert m.group(1) in sources, f"{module}::{fn} is not the router of any conditional edge in build.py"


def test_the_graph_modules_define_no_router_under_the_old_scheme():
    for module, functions in _module_functions().items():
        stray = [f for f in functions if f.startswith(("route_after_", "should_continue"))]
        assert not stray, f"{module} defines {stray}: a router is `graph_edge_route_after_<node>`"


def test_a_factory_returns_a_function_named_graph_node_followed_by_its_node():
    for path in sorted(GRAPH_DIR.glob("*.py")):
        for fn in ast.parse(path.read_text(encoding="utf-8")).body:
            if not (isinstance(fn, ast.FunctionDef) and fn.name.startswith("make_graph_node_")):
                continue
            built = "graph_node_" + fn.name.removeprefix("make_graph_node_")
            inner = {n.name for n in ast.walk(fn) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            returned = {r.value.id for r in ast.walk(fn) if isinstance(r, ast.Return) and isinstance(r.value, ast.Name)}
            assert built in inner and built in returned, f"{path.name}::{fn.name} should define and return `{built}`"
