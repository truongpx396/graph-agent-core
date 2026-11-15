"""`agent_first_token_seconds`: what a person waits before they see anything (PRODUCTION_REVIEW.md R17).

Observed once per turn, at the first `token` event the stream sends, from the start of the graph run. The queue's own wait is a separate number.
"""
from types import SimpleNamespace

from langchain_core.messages import AIMessage, AIMessageChunk

from app.agent.runtime import stream as stream_module
from tests.conftest import _METRIC_READER


def _first_token_histogram():
    data = _METRIC_READER.get_metrics_data()
    for resource in (data.resource_metrics if data else []):
        for scope in resource.scope_metrics:
            for metric in scope.metrics:
                if metric.name == "agent_first_token_seconds":
                    return [(p.count, p.sum) for p in metric.data.data_points][-1]
    return (0, 0.0)


class _Graph:
    def __init__(self, events, final="The answer."):
        self._events, self._final = events, final

    async def astream_events(self, graph_input, config=None, version=None):
        for event in self._events:
            if isinstance(event, (int, float)):
                clock[0] += event  # time passing between two events
                continue
            yield event

    async def aget_state(self, cfg):
        return SimpleNamespace(next=(), tasks=(), values={"messages": [AIMessage(content=self._final)], "iterations": 1})


clock = [0.0]


def _token(text):
    return {
        "event": "on_chat_model_stream", "name": "ChatOpenAI", "metadata": {"langgraph_node": "agent"},
        "data": {"chunk": AIMessageChunk(content=text)},
    }


async def _run(monkeypatch, events, **kwargs):
    clock[0] = 100.0
    monkeypatch.setattr(stream_module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return [e async for e in stream_module._run_graph_stream(_Graph(events, **kwargs), {}, {}, None)]


async def test_it_is_observed_once_at_the_first_token_with_the_time_since_the_run_started(monkeypatch):
    before_count, before_sum = _first_token_histogram()

    await _run(monkeypatch, [2.5, _token("The "), 1.0, _token("answer "), 1.0, _token("is 42.")])

    count, total = _first_token_histogram()
    assert count == before_count + 1, "once per turn, not once per token"
    assert abs((total - before_sum) - 2.5) < 1e-6


async def test_a_turn_with_no_tokens_and_no_answer_observes_nothing(monkeypatch):
    """An error or an approval pause streams no text: there is nothing for a person to have waited for."""
    before = _first_token_histogram()[0]

    await _run(monkeypatch, [1.0], final="")

    assert _first_token_histogram()[0] == before


async def test_a_cached_or_rejected_answer_is_measured_too_since_it_is_what_a_person_first_sees(monkeypatch):
    """Those turns send their text as one synthetic token after the run; leaving them out would bias the figure upward."""
    before_count, before_sum = _first_token_histogram()

    events = await _run(monkeypatch, [0.4], final="A cached answer.")

    assert any(e == {"type": "token", "content": "A cached answer."} for e in events)
    count, total = _first_token_histogram()
    assert count == before_count + 1 and abs((total - before_sum) - 0.4) < 1e-6
