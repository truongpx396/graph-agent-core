"""A graph knows the version of what its model is told, and it follows that text to the cache and to a turn's trace (GRAPH_PATTERNS.md pattern 114).

Without it a prompt change was invisible afterwards: nothing on a trace said which wording had produced an answer, and the answer cache went on
replaying what the old prompt had said until the entry expired.
"""
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from app.agent.graph.build import GraphDeps
from app.agent.runtime import stream as stream_module
from app.core.prompt_rules import prompt_version
from app.ports.outbound.domains import AgentManifest
from tests.ecorp import build_graph

REPO = Path(__file__).resolve().parents[3]
SCHEMA = [{"type": "function", "function": {"name": "t", "description": "does one thing", "parameters": {}}}]


def test_the_version_follows_the_prompt_and_the_tool_descriptions():
    version = prompt_version("You are a.", SCHEMA)

    assert version == prompt_version("You are a.", [{"function": dict(SCHEMA[0]["function"]), "type": "function"}]), "key order says nothing"
    assert version != prompt_version("You are b.", SCHEMA)
    changed = [{"type": "function", "function": {**SCHEMA[0]["function"], "description": "does another thing"}}]
    assert version != prompt_version("You are a.", changed)
    assert len(version) == 12


def test_a_graph_carries_the_version_of_its_own_prompt():
    one = build_graph(GraphDeps(llm=None), manifest=AgentManifest("x", "You are one."))
    again = build_graph(GraphDeps(llm=None), manifest=AgentManifest("x", "You are one."))
    other = build_graph(GraphDeps(llm=None), manifest=AgentManifest("x", "You are two."))

    assert one.prompt_version == again.prompt_version != other.prompt_version


def test_every_domains_version_is_the_same_in_every_process():
    """A version that depended on set ordering would give each worker its own cache partition."""
    script = (
        "from app.agent.graph.build import GraphDeps, build_graph\n"
        "from app.domains.registry import DOMAINS\n"
        "print(sorted((name, build_graph(m, p, GraphDeps(llm=object())).prompt_version) for name, (m, p) in DOMAINS.items()))\n"
    )
    seen = {
        subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO,
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": os.pathsep.join([str(REPO), str(REPO / "packages/billing/src")])},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip().splitlines()[-1]
        for seed in ("1", "2")
    }

    assert len(seen) == 1, seen


async def test_a_turns_run_carries_the_version_so_every_span_does(monkeypatch):
    seen: dict = {}

    async def fake_stream(graph, graph_input, cfg, trace, cancel_check=None):
        seen.update(cfg)
        yield {"type": "done"}

    monkeypatch.setattr(stream_module, "_run_graph_stream", fake_stream)
    monkeypatch.setattr(stream_module, "_open_trace", lambda *a, **k: (None, []))
    graph = SimpleNamespace(prompt_version="abc123abc123")

    events = [e async for e in stream_module._run_turn("chat-turn-stream", graph, "thread", {}, lambda: None, "hi", None)]

    assert events == [{"type": "done"}]
    assert seen["metadata"] == {"prompt_version": "abc123abc123"}
