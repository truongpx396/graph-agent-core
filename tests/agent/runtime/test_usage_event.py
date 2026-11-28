"""The `usage` event (specs/025, COST_GOVERNANCE.md 120): what a turn's model calls used, with unknown never reported as smaller."""
from decimal import Decimal

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from app.agent.cost import metering, pricing
from app.agent.graph.build import GraphDeps
from app.agent.graph.state import concat_or_reset
from app.agent.runtime import stream as stream_module
from app.agent.runtime.usage import usage_event
from app.ports.outbound import models
from app.ports.outbound.domains import AgentManifest
from tests.agent.fakes import Domain, cfg, queue_llm
from tests.conftest import TEST_CTX
from tests.ecorp import build_graph

ANSWER = "Here is the answer you asked me for just now."


def _entry(kind="chat", alias="chat", input_tokens=10, output_tokens=2, cost=0.5):
    return {"alias": alias, "kind": kind, "input_tokens": input_tokens, "output_tokens": output_tokens, "cost_usd": cost}


class _Catalog:
    async def resolve(self, alias):
        return {"chat": "ollama_chat/small"}.get(alias)


@pytest.fixture(autouse=True)
def catalog():
    with models.model_catalog.overriding(_Catalog()):
        yield


async def test_the_totals_are_of_every_call_and_the_model_is_the_one_that_answered():
    event = await usage_event([_entry("compaction", cost=0.25), _entry("subagent", alias="specialist"), _entry(), _entry("followups", alias="other")])

    assert event == {
        "type": "usage", "model": "ollama_chat/small", "route": "local",
        "input_tokens": 40, "output_tokens": 8, "cost_usd": 1.75, "model_calls": 4,
    }


async def test_one_unknown_part_makes_that_total_unknown_not_smaller():
    event = await usage_event([_entry(), _entry(cost=None)])

    assert (event["cost_usd"], event["input_tokens"], event["model_calls"]) == (None, 20, 2)


async def test_a_delegated_run_that_failed_makes_the_totals_unknown_and_is_not_counted_as_a_call():
    event = await usage_event([_entry(), metering.UNKNOWN_USAGE])

    assert (event["input_tokens"], event["output_tokens"], event["cost_usd"], event["model_calls"]) == (None, None, None, 1)


async def test_a_turn_no_model_answered_used_nothing_and_names_no_model():
    event = await usage_event([])

    assert (event["model"], event["route"], event["input_tokens"], event["cost_usd"], event["model_calls"]) == (None, None, 0, 0.0, 0)


async def test_a_gateway_that_cannot_name_the_model_leaves_its_alias():
    assert (await usage_event([_entry(alias="unlisted")]))["model"] == "unlisted"


def test_a_call_the_provider_reported_no_usage_for_is_unknown_not_free():
    priced = pricing.PricedCall(0, 0, 0, 0, 0.0, None)
    silent = metering.MeteredCall(response=None, usage={}, cost_usd=0.0, priced=priced, model_alias="chat", kind="chat")

    assert silent.usage_entry == {"alias": "chat", "kind": "chat", "input_tokens": None, "output_tokens": None, "cost_usd": None}


def test_the_entries_are_appended_and_reset_by_none():
    assert concat_or_reset([_entry()], [_entry()]) == [_entry(), _entry()]
    assert concat_or_reset([_entry()], None) == []


def _reported(content="", tool_calls=(), tokens=(100, 10)):
    usage = {"input_tokens": tokens[0], "output_tokens": tokens[1], "total_tokens": sum(tokens)}
    return AIMessage(content=content, tool_calls=list(tool_calls), usage_metadata=usage)


async def test_a_turn_paused_for_approval_reports_the_calls_before_and_after_the_pause_once(monkeypatch):
    """The state carries the entries across the pause, so the resume's `usage` is the whole turn's."""

    @tool
    async def save_note(text: str) -> str:
        """Save a note."""
        return "saved"

    async def priced_at_a_cent_each(alias, usage):
        return pricing.PricedCall(usage["input_tokens"], usage["output_tokens"], 0, usage["total_tokens"], float(Decimal("0.01")), None)

    monkeypatch.setattr(pricing, "price_call", priced_at_a_cent_each)
    llm = queue_llm(_reported(tool_calls=[{"name": "save_note", "args": {"text": "x"}, "id": "c1"}]), _reported(ANSWER, tokens=(200, 20)))
    manifest = AgentManifest(name="notes", system_prompt="You keep notes.", prefetch="never", suggest_followups=False)
    graph = build_graph(GraphDeps(llm=llm), manifest=manifest, domain=Domain([save_note], {"save_note": "mutating"}))

    async def this_graph(*args, **kwargs):
        return graph

    monkeypatch.setattr(stream_module.runtime_module, "init_graph_async", this_graph)
    config = cfg()
    first = [e async for e in stream_module._run_graph_stream(graph, {"messages": [HumanMessage(content="note x")]}, config, trace=None)]
    assert [e["type"] for e in first] == ["approval_required"], "a pause reports no usage: the turn is not finished"

    resumed = [e async for e in stream_module.astream_events_resume(config["configurable"]["thread_id"], approved=True, ctx=TEST_CTX)]

    (usage,) = [e for e in resumed if e["type"] == "usage"]
    assert (usage["input_tokens"], usage["output_tokens"], usage["model_calls"]) == (300, 30, 2)
    assert usage["cost_usd"] == pytest.approx(0.02)
    assert [e["type"] for e in resumed][-2:] == ["usage", "done"]


async def test_a_new_turn_starts_from_nothing():
    manifest = AgentManifest(name="plain", system_prompt="You answer.", prefetch="never", suggest_followups=False)
    graph = build_graph(GraphDeps(llm=queue_llm(_reported(ANSWER), _reported(ANSWER)), cache_get=_never), manifest=manifest, domain=Domain([], {}))
    config = cfg()

    for question in ("one", "two"):
        events = [e async for e in stream_module._run_graph_stream(graph, {"messages": [HumanMessage(content=question)]}, config, trace=None)]

        (usage,) = [e for e in events if e["type"] == "usage"]
        assert (usage["model_calls"], usage["input_tokens"]) == (1, 100)


async def _never(ctx, query):
    return None


async def test_a_delegation_that_times_out_is_noted_as_lost_usage():
    with metering.lost_usage() as lost:
        metering.note_lost_usage()
    metering.note_lost_usage()  # outside a tool call: nothing to note it on

    assert lost == [True]
