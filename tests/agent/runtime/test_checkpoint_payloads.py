"""What loading a saved conversation runs: the payload shape of CVE-2025-64439 no longer does. The payload here only makes a directory.

langgraph-checkpoint 2.x read a JSON checkpoint by importing and CALLING whatever an object shaped `{"lc": 2, "type": "constructor", "id": [...]}`
named. Its own way of writing JSON (a fallback for a value with a broken Unicode character) did not trigger with the ormsgpack installed here,
which raised a different error instead and crashed the turn: the first test holds both ends of that.

NOT closed: a crafted msgpack row (the format the app writes) still imports and calls what it names on langgraph-checkpoint 3.0.1
(CVE-2026-28277, CVE-2026-48775), so whoever can write to the checkpoint database can still run code in the app. The last test records that, and
fails once an upgrade or a restricted deserializer closes it: then turn it round (GRAPH_PATTERNS.md, "Known gaps").
"""
import json

import ormsgpack
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.serde import jsonplus
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agent.graph.build import GraphDeps
from tests.agent.fakes import cfg, scripted_llm
from tests.ecorp import build_graph

BROKEN_CHARACTER = "\udc80"  # a lone surrogate: valid in JSON and in a Python str, not encodable as UTF-8


def _payload(marker) -> dict:
    return {"lc": 2, "type": "constructor", "id": ["os", "makedirs"], "args": [str(marker)]}


def _model(marker):
    """Calls a mutating tool, so the turn pauses for approval and is saved with these arguments in it."""
    call = AIMessage(content="", tool_calls=[{
        "name": "add_note", "id": "call-1",
        "args": {"title": "t", "content": f"note {BROKEN_CHARACTER}", "topic": "company", "extra": _payload(marker)},
    }])
    return scripted_llm(lambda messages: call)


async def test_a_broken_character_in_a_tool_argument_neither_breaks_the_turn_nor_runs_anything(tmp_path):
    marker = tmp_path / "ran"
    database, config = str(tmp_path / "checkpoints.db"), cfg()
    async with AsyncSqliteSaver.from_conn_string(database) as saver:
        await build_graph(GraphDeps(llm=_model(marker)), checkpointer=saver).ainvoke({"messages": [HumanMessage(content="remember this")]}, config)
    # What the next request does, in this process or another one: read the conversation back.
    async with AsyncSqliteSaver.from_conn_string(database) as saver:
        state = await build_graph(GraphDeps(llm=_model(marker)), checkpointer=saver).aget_state(config)

    assert state.next == ("human_approval",), "the turn was saved, paused for approval, and read back"
    assert not marker.exists(), "loading the conversation ran code named in a tool call's arguments"


def test_a_stored_json_checkpoint_in_that_shape_runs_nothing(tmp_path):
    marker = tmp_path / "ran"
    JsonPlusSerializer().loads_typed(("json", json.dumps({"messages": [_payload(marker)]}).encode()))
    assert not marker.exists()


def test_known_gap_a_crafted_msgpack_checkpoint_still_runs_what_it_names(tmp_path):
    """A record of an OPEN hole, not a behaviour to keep: when this fails, the hole is closed."""
    marker = tmp_path / "ran"
    construct = ormsgpack.Ext(jsonplus.EXT_CONSTRUCTOR_SINGLE_ARG, ormsgpack.packb(("os", "makedirs", str(marker))))
    JsonPlusSerializer().loads_typed(("msgpack", ormsgpack.packb(construct)))
    assert marker.exists()
