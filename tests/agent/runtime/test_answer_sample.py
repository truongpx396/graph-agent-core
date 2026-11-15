"""`answer_sample` (`app/agent/runtime/answer_sample.py`, PRODUCTION_REVIEW.md R28): yesterday's answers, checked for grounding, as counts.

The checks are the ones the graph runs on every turn, so `judge` is tested on real data against the real functions. The sample reads across tenants (an operator job), so the
property that matters most is what does NOT leave it: no answer text, no ids, in a metric or a log line."""
import logging
from datetime import UTC, date, datetime, timedelta

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import empty_checkpoint

from app.agent.runtime import answer_sample
from app.core import metrics
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.sessions import session_store
from tests.conftest import metric_value
from tests.fakes.sessions import InMemoryCheckpointing, InMemorySessionStore

DAY = date(2026, 11, 14)
IN_DAY = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
REFUND = [{"marker": "[1]", "text": "Refunds are issued within thirty days of purchase for unused items."}]
SENTINEL = "ZXQ-secret-sentence-7781"


# --- judge: the three real checks ---------------------------------------------------------------------------------------------------------------


def test_an_answer_that_cites_a_real_source_it_actually_drew_on_is_clean():
    assert answer_sample.judge("Refunds are issued within thirty days of purchase [1].", REFUND) == []


def test_a_source_number_nobody_offered_is_an_ungrounded_marker():
    assert "ungrounded_marker" in answer_sample.judge("Refunds take thirty days [2].", REFUND)


def test_a_source_used_without_its_marker_is_uncited():
    assert answer_sample.judge("Refunds are issued within thirty days of purchase for unused items.", REFUND) == ["uncited"]


def test_a_marker_on_a_sentence_unrelated_to_that_source_is_misattributed():
    assert answer_sample.judge("The warehouse opens at nine every weekday morning [1].", REFUND) == ["misattributed"]


# --- final_answer --------------------------------------------------------------------------------------------------------------------------------


def test_the_final_answer_is_the_last_message_when_it_is_the_models_and_says_something():
    assert answer_sample.final_answer([HumanMessage(content="q"), AIMessage(content="the answer [1]")]) == "the answer [1]"


@pytest.mark.parametrize(
    "messages",
    [
        [],
        [HumanMessage(content="q")],                                                                                         # not answered yet
        [HumanMessage(content="q"), AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}])],               # ended on a tool call
        [HumanMessage(content="q"), AIMessage(content="", tool_calls=[{"name": "t", "args": {}, "id": "c1"}]), ToolMessage(content="r", tool_call_id="c1")],
        [HumanMessage(content="q"), AIMessage(content="   ")],                                                               # said nothing
        [AIMessage(content="an answer with no question before it")],
        [HumanMessage(content="q"), AIMessage(content=[{"type": "text", "text": "parts"}])],                                 # not plain text
    ],
    ids=["empty", "unanswered", "tool call", "tool result", "blank", "no question", "content parts"],
)
def test_a_conversation_that_did_not_end_on_an_answer_has_nothing_to_judge(messages):
    assert answer_sample.final_answer(messages) is None


# --- the sample ---------------------------------------------------------------------------------------------------------------------------------


async def _save(saver, thread_id, messages, citations):
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    checkpoint = {**empty_checkpoint(), "channel_values": {"messages": messages, "citations": citations}, "channel_versions": {"messages": "1", "citations": "1"}}
    await saver.aput(config, checkpoint, {"source": "loop", "step": 1, "writes": {}, "parents": {}}, {"messages": "1", "citations": "1"})


def _answered(text, citations):
    return [HumanMessage(content="a question"), AIMessage(content=text)], citations


@pytest.fixture
async def world():
    sessions, saver = InMemorySessionStore(), InMemoryCheckpointing()
    with session_store.overriding(sessions), checkpointing.overriding(saver):
        yield sessions, saver


async def _seed(world, thread, text, citations, *, when=IN_DAY, tenant="acme"):
    sessions, saver = world
    sessions.add(thread, tenant=tenant, last_active_at=when)
    messages, cites = _answered(text, citations)
    await _save(await saver.open(), thread, messages, cites)


def _before():
    return {o: metric_value(metrics.agent_answer_sample_total, outcome=o) for o in ("judged", "no_citations", "unreadable")}, {
        f: metric_value(metrics.agent_answer_sample_finding_total, finding=f) for f in answer_sample.FINDINGS
    }


async def test_each_conversation_is_judged_skipped_for_having_no_citations_or_counted_unreadable(world):
    sessions, _ = world
    await _seed(world, "clean", "Refunds are issued within thirty days of purchase [1].", REFUND)
    await _seed(world, "bad", "Refunds take thirty days [2].", REFUND)
    await _seed(world, "general", "Two plus two is four.", [])                      # no sources offered: nothing to check
    sessions.add("never-saved", last_active_at=IN_DAY)                               # a row with no checkpoint behind it
    outcomes, findings = _before()

    result = await answer_sample.sample_answers(day=DAY, size=50)

    assert (result.sampled, result.judged, result.no_citations, result.unreadable) == (4, 2, 1, 1)
    assert result.findings["ungrounded_marker"] == 1 and result.findings["misattributed"] == 0
    assert metric_value(metrics.agent_answer_sample_total, outcome="judged") == outcomes["judged"] + 2
    assert metric_value(metrics.agent_answer_sample_total, outcome="no_citations") == outcomes["no_citations"] + 1
    assert metric_value(metrics.agent_answer_sample_total, outcome="unreadable") == outcomes["unreadable"] + 1
    assert metric_value(metrics.agent_answer_sample_finding_total, finding="ungrounded_marker") == findings["ungrounded_marker"] + 1


async def test_only_conversations_last_active_on_that_day_are_sampled(world):
    await _seed(world, "in", "Refunds are issued within thirty days of purchase [1].", REFUND)
    await _seed(world, "before", "x [1].", REFUND, when=IN_DAY - timedelta(days=1))
    await _seed(world, "after", "x [1].", REFUND, when=IN_DAY + timedelta(days=1))
    await _seed(world, "midnight", "x [1].", REFUND, when=datetime(2026, 11, 15, 0, 0, tzinfo=UTC))  # the end of the window is exclusive

    assert (await answer_sample.sample_answers(day=DAY, size=50)).sampled == 1


async def test_the_sample_is_at_most_the_size_asked_for(world):
    for n in range(10):
        await _seed(world, f"t{n}", "Refunds are issued within thirty days of purchase [1].", REFUND)

    assert (await answer_sample.sample_answers(day=DAY, size=4)).sampled == 4


async def test_it_can_be_limited_to_the_tenants_that_agreed(world):
    await _seed(world, "a", "Refunds are issued within thirty days of purchase [1].", REFUND, tenant="acme")
    await _seed(world, "g", "Refunds are issued within thirty days of purchase [1].", REFUND, tenant="globex")

    assert (await answer_sample.sample_answers(day=DAY, size=50, tenants=("acme",))).sampled == 1
    assert (await answer_sample.sample_answers(day=DAY, size=50)).sampled == 2


async def test_a_conversation_that_cannot_be_read_is_counted_and_does_not_stop_the_rest(world):
    sessions, saver = world
    await _seed(world, "good", "Refunds are issued within thirty days of purchase [1].", REFUND)
    sessions.add("broken", last_active_at=IN_DAY)
    real = (await saver.open()).aget_tuple

    async def flaky(config):
        if config["configurable"]["thread_id"] == "broken":
            raise ConnectionError("the checkpointer went away")
        return await real(config)

    (await saver.open()).aget_tuple = flaky  # type: ignore[method-assign]

    result = await answer_sample.sample_answers(day=DAY, size=50)

    assert (result.judged, result.unreadable) == (1, 1)


async def test_no_answer_text_and_no_id_leaves_the_sample_in_a_metric_or_a_log_line(world, caplog):
    await _seed(world, f"thread-{SENTINEL}", f"{SENTINEL} is claimed here [9].", REFUND, tenant=f"tenant-{SENTINEL}")

    with caplog.at_level(logging.DEBUG):
        await answer_sample.sample_answers(day=DAY, size=50)

    assert SENTINEL not in caplog.text, "an answer, a tenant or a thread id reached a log line"
    assert SENTINEL not in " ".join(repr(r.__dict__) for r in caplog.records), "...or a log record's structured fields (`extra`), which the text above does not show"
    for counter in (metrics.agent_answer_sample_total, metrics.agent_answer_sample_finding_total):
        assert SENTINEL not in repr(counter.__dict__), "nothing about a conversation is a metric attribute"


async def test_a_day_with_no_conversations_samples_nothing(world):
    result = await answer_sample.sample_answers(day=DAY, size=50)

    assert (result.sampled, result.judged) == (0, 0)


def test_the_window_is_one_utc_day():
    start, end = answer_sample.day_window(DAY)

    assert (start, end) == (datetime(2026, 11, 14, tzinfo=UTC), datetime(2026, 11, 15, tzinfo=UTC))
