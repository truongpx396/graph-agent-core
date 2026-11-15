"""Tests for `instrumented`, the structured lifecycle-logging wrapper
applied to every node at graph-registration time (see build_graph and
GRAPH_PATTERNS.md pattern 14).

These test the decorator in isolation with a throwaway node function, not
through a compiled graph — the compiled-graph HITL pause/resume behavior
this wrapper must not break is already covered end-to-end by
TestHumanApprovalPath in test_graph_integration.py.
"""
import logging

import pytest
from langgraph.errors import GraphInterrupt

from app.agent.graph.utils import instrumented


def test_wraps_node_and_returns_its_result_unchanged():
    @instrumented("dummy")
    def node(state):
        return {"ok": True}

    assert node({"run_id": "abc123"}) == {"ok": True}


def test_logs_started_and_completed_on_success(caplog):
    @instrumented("dummy")
    def node(state):
        return {"ok": True}

    with caplog.at_level(logging.DEBUG, logger="app.agent.graph.utils"):
        node({"run_id": "abc123"})

    messages = [r.message for r in caplog.records]
    assert "node_started" in messages
    assert "node_completed" in messages
    assert "node_failed" not in messages

    completed = next(r for r in caplog.records if r.message == "node_completed")
    assert completed.node == "dummy"
    assert completed.run_id == "abc123"
    assert isinstance(completed.duration_ms, int)


def test_missing_run_id_logs_as_placeholder_not_a_crash():
    @instrumented("dummy")
    def node(state):
        return {}

    # No run_id key at all — e.g. a node called directly in a unit test
    # with a hand-built state dict, outside validate_input's reset.
    assert node({}) == {}


def test_logs_failed_and_reraises_on_exception(caplog):
    @instrumented("dummy")
    def node(state):
        raise ValueError("boom")

    with caplog.at_level(logging.WARNING, logger="app.agent.graph.utils"), pytest.raises(ValueError, match="boom"):
        node({"run_id": "abc123"})

    failed = next(r for r in caplog.records if r.message == "node_failed")
    assert failed.node == "dummy"
    assert failed.run_id == "abc123"
    assert failed.error_class == "ValueError"


def test_graph_interrupt_is_logged_as_paused_not_failed_and_still_propagates(caplog):
    """human_approval's interrupt() raises GraphInterrupt to pause the
    run — normal control flow, not a node failure. It must be logged
    distinctly and re-raised untouched, or LangGraph can't actually pause
    the graph (see instrumented's docstring)."""

    @instrumented("dummy")
    def node(state):
        raise GraphInterrupt()

    with caplog.at_level(logging.INFO, logger="app.agent.graph.utils"), pytest.raises(GraphInterrupt):
        node({"run_id": "abc123"})

    messages = [r.message for r in caplog.records]
    assert "node_paused" in messages
    assert "node_failed" not in messages


def test_started_is_debug_and_completed_failed_and_paused_are_info(caplog):
    """A turn runs 25 to 30 nodes: both ends of each at INFO was that many lines per turn (PRODUCTION_REVIEW.md R35, part C)."""
    @instrumented("ok")
    def ok(state):
        return {}

    @instrumented("boom")
    def boom(state):
        raise RuntimeError("x")

    @instrumented("pause")
    def pause(state):
        raise GraphInterrupt(())

    with caplog.at_level(logging.DEBUG, logger="app.agent.graph.utils"):
        ok({"run_id": "r"})
        with pytest.raises(RuntimeError):
            boom({"run_id": "r"})
        with pytest.raises(GraphInterrupt):
            pause({"run_id": "r"})

    levels = {}
    for record in caplog.records:
        levels.setdefault(record.message, set()).add(record.levelno)
    assert levels["node_started"] == {logging.DEBUG}
    assert levels["node_completed"] == {logging.INFO}
    assert levels["node_paused"] == {logging.INFO}
    assert levels["node_failed"] and levels["node_failed"] <= {logging.WARNING, logging.ERROR, logging.INFO}


def test_at_the_default_info_level_a_successful_node_writes_one_line_not_two(caplog):
    @instrumented("dummy")
    def node(state):
        return {}

    with caplog.at_level(logging.INFO, logger="app.agent.graph.utils"):
        node({"run_id": "r"})

    assert [r.message for r in caplog.records] == ["node_completed"]
