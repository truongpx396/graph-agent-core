"""What a person SEES while an answer is judged for a second look (docs/GRAPH_PATTERNS.md pattern 105).

An answer that says the pre-fetched documents lack it is taken back and asked again. Streamed as usual, a person read it and then watched it being
replaced, so the stream holds it (`runtime/held_answer.py`). `_shown` replays the events the way a client renders them (a `retry` clears the answer), and
`_ever_sent` is every piece of text that reached the client at all.
"""
import json
import re
import uuid
from types import SimpleNamespace

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

from app.agent.graph.build import GraphDeps
from app.agent.graph.stream_events import STREAM_EVENT
from app.agent.runtime import core as runtime_module
from app.agent.runtime import stream as stream_module
from app.agent.runtime.held_answer import HeldAnswer
from tests.conftest import TEST_CTX
from tests.ecorp import build_graph

SOURCES = [
    {"marker": "[1]", "doc_id": "d1", "title": "Support", "text": "Ecorp's support hours are 9am to 5pm on weekdays, and support is free for all open-source users.", "score": -3.7},
    {"marker": "[2]", "doc_id": "d2", "title": "About", "text": "Ecorp was founded in 2021 and builds offline developer tools.", "score": 0.6},
]
GAP = "The retrieved document does not provide specific information about who works on Ecorp's engineering team [1]."
GAP_SHOWN = GAP.replace(" [1]", "")


class TestHeldAnswer:
    def _feed(self, held, text):
        return "".join(held.feed(piece) for piece in re.findall(r"\S+\s*", text))

    def test_with_nothing_held_every_token_goes_straight_through(self):
        assert HeldAnswer().feed("Hello ") == "Hello "

    def test_an_ordinary_first_sentence_is_released_once_it_is_complete_and_the_rest_streams(self):
        held = HeldAnswer()
        held.hold("first_sentence")
        assert [held.feed(p) for p in ["Support ", "is ", "free."]] == ["", "", ""]  # not complete until something follows the stop
        assert held.feed(" It ") == "Support is free. It "
        assert held.feed("is.") == "is."
        assert not held.pending

    def test_an_admission_keeps_the_whole_answer(self):
        held = HeldAnswer()
        held.hold("first_sentence")
        assert self._feed(held, GAP + " Could you say more? ") == ""
        assert held.call_ended(called_a_tool=False) == ""
        assert held.pending

    def test_an_answer_with_no_full_stop_is_judged_when_the_call_ends(self):
        held = HeldAnswer()
        held.hold("first_sentence")
        assert self._feed(held, "Support is free") == ""
        assert held.call_ended(called_a_tool=False) == "Support is free"

    def test_a_long_run_without_a_full_stop_does_not_hold_the_whole_answer(self):
        held = HeldAnswer()
        held.hold("first_sentence")
        assert self._feed(held, "item " * 80).startswith("item item")

    def test_text_written_beside_a_tool_call_is_sent(self):
        held = HeldAnswer()
        held.hold("kept")
        assert self._feed(held, "Let me check. ") == ""
        assert held.call_ended(called_a_tool=True) == "Let me check. "

    def test_a_second_look_is_held_until_someone_says_so(self):
        held = HeldAnswer()
        held.hold("kept")
        assert self._feed(held, "Four people work there. Anything else? ") == ""
        assert held.call_ended(called_a_tool=False) == ""
        assert held.release() == "Four people work there. Anything else? "


class _Streams(GenericFakeChatModel):
    """Streams each scripted reply word by word, ending with its tool calls if it has any. Anything asked beyond the script (follow-ups) gets "[]"."""

    script: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _next(self):
        return self.script.pop(0) if self.script else AIMessage(content="[]")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next())])

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        reply = self._next()
        for piece in re.findall(r"\S+\s*", reply.content):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))
        if reply.tool_calls:
            calls = [{"name": c["name"], "args": json.dumps(c["args"]), "id": c["id"], "index": i} for i, c in enumerate(reply.tool_calls)]
            yield ChatGenerationChunk(message=AIMessageChunk(content="", tool_call_chunks=calls))


def _calculate():
    """Stands in for query_employees: a real tool round that needs no database."""
    return AIMessage(content="", tool_calls=[{"name": "calculator", "args": {"expression": "2+2"}, "id": "c1"}])


async def _turn(monkeypatch, *script):
    async def search(query, ctx):
        return "\n".join(f"{s['marker']} {s['text']}" for s in SOURCES), list(SOURCES)

    graph = build_graph(GraphDeps(llm=_Streams(messages=iter([]), script=list(script)), search_docs=search))

    async def fake_init():
        return graph

    monkeypatch.setattr(runtime_module, "init_graph_async", fake_init)
    return [event async for event in stream_module.astream_events_turn("Who works on the engineering team?", str(uuid.uuid4()), TEST_CTX)]


def _shown(events):
    """The answer as a client has it on screen after the last event."""
    text = ""
    for event in events:
        if event["type"] == "retry":
            text = ""
        elif event["type"] == "token":
            text += event["content"]
    return text


def _ever_sent(events):
    return "".join(e["content"] for e in events if e["type"] == "token")


def _types(events):
    return [e["type"] for e in events]


class TestWhatAPersonSees:
    async def test_an_admission_that_leads_to_a_tool_is_never_shown(self, monkeypatch):
        events = await _turn(monkeypatch, AIMessage(content=GAP), _calculate(), AIMessage(content="Four engineers work there [1]."))

        assert "does not provide" not in _ever_sent(events)
        assert _shown(events) == "Four engineers work there."  # once, clean: no marker that points nowhere
        assert "retry" not in _types(events)  # nothing was on screen to revise
        assert _types(events).index("tool_start") < _types(events).index("token")
        assert events[-1] == {"type": "done"}
        assert "hold" not in _types(events) and "release" not in _types(events)  # between the graph and the runtime only

    async def test_the_first_answer_is_shown_once_when_the_second_look_uses_no_tool(self, monkeypatch):
        events = await _turn(monkeypatch, AIMessage(content=GAP), AIMessage(content="You would typically need to submit a ticket. Then wait."))

        assert "typically" not in _ever_sent(events)  # generic steps from memory never reach the person
        assert _shown(events) == GAP_SHOWN
        assert _ever_sent(events) == GAP_SHOWN  # sent once
        assert "retry" not in _types(events)

    async def test_an_ordinary_documents_answer_arrives_whole_and_still_streams(self, monkeypatch):
        answer = "Ecorp's support hours are 9am to 5pm on weekdays [1]. Support is free for all open-source users [1]."
        events = await _turn(monkeypatch, AIMessage(content=answer))

        assert _shown(events) == answer
        assert sum(e["type"] == "token" for e in events) > 2  # the first sentence in one piece, then token by token
        assert "retry" not in _types(events)

    async def test_a_partial_answer_that_opens_with_an_admission_is_kept_and_shown(self, monkeypatch):
        answer = "The documents do not mention weekend cover. Ecorp's support hours are 9am to 5pm on weekdays [1]."
        events = await _turn(monkeypatch, AIMessage(content=answer))

        assert _shown(events) == answer
        assert _ever_sent(events) == answer

    async def test_switched_off_the_admission_streams_as_it_is_written_and_nothing_is_asked_again(self, monkeypatch):
        monkeypatch.setattr("app.agent.graph.build.CONTEXT_GAP_REASK_ENABLED", False)
        events = await _turn(monkeypatch, AIMessage(content=GAP), AIMessage(content="never asked for"))

        assert _shown(events) == GAP_SHOWN
        assert sum(e["type"] == "token" for e in events) > 1  # streamed, not held
        assert "never asked for" not in _ever_sent(events)


class _Graph:
    """Replays fixed `astream_events` output, with `final` as the checkpoint's last message."""

    def __init__(self, events, final):
        self._events, self._final = events, final

    async def astream_events(self, graph_input, config=None, version="v2"):
        for event in self._events:
            yield event

    async def aget_state(self, cfg):
        return SimpleNamespace(values={"messages": [self._final]}, next=(), tasks=[])


def _announce(**data):
    return {"event": "on_custom_event", "name": STREAM_EVENT, "run_id": "r", "metadata": {}, "data": data}


def _token(text):
    return {"event": "on_chat_model_stream", "run_id": "r", "name": "ChatOpenAI", "metadata": {"langgraph_node": "agent"}, "data": {"chunk": AIMessageChunk(content=text)}}


@pytest.mark.parametrize(
    ("final", "expected"),
    [
        # A budget exit replaces an answer without announcing it: the person gets what the checkpoint has, never the held draft.
        (AIMessage(content="I couldn't finish that within the limits."), "I couldn't finish that within the limits."),
        # Nothing took it back: the checkpoint has the held text.
        (AIMessage(content="draft words"), "draft words"),
    ],
)
async def test_text_still_held_when_the_turn_ends_is_replaced_by_what_the_checkpoint_says(final, expected):
    graph = _Graph([_announce(type="hold", until="kept"), _token("draft "), _token("words")], final)
    events = [e async for e in stream_module._run_graph_stream(graph, {}, {"configurable": {"thread_id": "t", "ctx": TEST_CTX}}, trace=None)]

    assert _ever_sent(events) == expected


async def test_a_retry_still_reaches_the_client_when_something_was_already_on_screen():
    graph = _Graph(
        [_token("Looking. "), _announce(type="hold", until="kept"), _token("held "), _announce(type="retry"), _token("Done.")], AIMessage(content="Done.")
    )
    events = [e async for e in stream_module._run_graph_stream(graph, {}, {"configurable": {"thread_id": "t", "ctx": TEST_CTX}}, trace=None)]

    assert [e["type"] for e in events[:3]] == ["token", "retry", "token"]
    assert _shown(events) == "Done."
    assert "held" not in _ever_sent(events)
