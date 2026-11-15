"""Tests for app/agent/runtime/stream.py::get_session_messages — the session switcher's
transcript replay (item #9). Bypasses the real durable checkpointer via a
monkeypatched init_graph_async (same pattern
tests/agent/runtime/test_streaming_terminal_events.py's `_events_for` helper already
uses) since only the message-filtering/normalization logic is under test
here, not checkpointer machinery (covered separately by
tests/agent/runtime/test_durable_checkpoint.py).
"""

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.graph.messages import COMPACTION_MARKER_KEY
from app.agent.runtime import core as agent_module
from app.agent.runtime import stream as stream_module


class _FakeState:
    def __init__(self, values):
        self.values = values


class _FakeGraph:
    def __init__(self, messages):
        self._messages = messages

    async def aget_state(self, cfg):
        return _FakeState({"messages": self._messages})


async def _get(messages, monkeypatch):
    async def fake_init_graph_async():
        return _FakeGraph(messages)

    monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
    return await stream_module.get_session_messages("t1")


async def test_human_and_ai_messages_become_user_and_assistant_roles(monkeypatch):
    result = await _get(
        [
            HumanMessage(content="What is the refund policy?"),
            AIMessage(content="30 days, no questions asked."),
        ],
        monkeypatch,
    )
    assert result == [
        {"index": 0, "role": "user", "text": "What is the refund policy?"},
        {"index": 1, "role": "assistant", "text": "30 days, no questions asked."},
    ]


async def test_system_and_tool_messages_are_omitted(monkeypatch):
    """A replay of the conversational back-and-forth a user saw — not a
    full forensic trace of every SystemMessage/ToolMessage the graph
    internally exchanged (that's what Langfuse is for)."""
    result = await _get(
        [
            SystemMessage(content="You are a helpful assistant."),
            HumanMessage(content="remember our refund policy"),
            AIMessage(
                content="",
                tool_calls=[{"name": "add_note", "args": {}, "id": "c1"}],
            ),
            ToolMessage(content="Note saved.", tool_call_id="c1"),
            AIMessage(content="I've saved that note for you."),
        ],
        monkeypatch,
    )
    assert result == [
        {"index": 0, "role": "user", "text": "remember our refund policy"},
        {"index": 1, "role": "assistant", "text": "I've saved that note for you."},
    ]


async def test_an_ai_message_with_no_text_is_skipped_not_shown_as_an_empty_bubble(monkeypatch):
    """The pure-tool-calling AIMessage above (empty content, only
    tool_calls) is exactly this case — asserted again here in isolation
    so a future reader sees the rule on its own, not just inferred from
    the combined scenario above."""
    result = await _get(
        [
            HumanMessage(content="what is 12 * 7?"),
            AIMessage(content="", tool_calls=[{"name": "calculator", "args": {}, "id": "c1"}]),
            ToolMessage(content="84", tool_call_id="c1"),
            AIMessage(content="12 times 7 is 84."),
        ],
        monkeypatch,
    )
    assert result == [
        {"index": 0, "role": "user", "text": "what is 12 * 7?"},
        {"index": 1, "role": "assistant", "text": "12 times 7 is 84."},
    ]


async def test_multimodal_content_list_is_flattened_to_text(monkeypatch):
    """An attached image (GRAPH_PATTERNS.md pattern 44) rides alongside
    text in a list-shaped .content — the replay should show only the
    text part, same normalization _run_graph_stream's token handling
    already applies via the shared _text_content helper."""
    result = await _get(
        [
            HumanMessage(
                content=[
                    {"type": "text", "text": "what's in this image?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ]
            ),
            AIMessage(content="A photo of a cat."),
        ],
        monkeypatch,
    )
    assert result == [
        {"index": 0, "role": "user", "text": "what's in this image?"},
        {"index": 1, "role": "assistant", "text": "A photo of a cat."},
    ]


async def test_empty_thread_returns_an_empty_list(monkeypatch):
    assert await _get([], monkeypatch) == []


async def test_a_compaction_marker_surfaces_as_a_system_role(monkeypatch):
    """The ONE exception to "SystemMessages are omitted" (see
    test_system_and_tool_messages_are_omitted above): a compact_history
    breadcrumb (app/agent/graph/core.py's COMPACTION_MARKER_KEY) is deliberately meant
    for a human replaying this session to see — unlike the seeded base
    prompt, which stays hidden."""
    result = await _get(
        [
            SystemMessage(content="You are a helpful assistant."),
            HumanMessage(content="question 1?"),
            AIMessage(content="answer 1."),
            SystemMessage(
                content="[1 earlier turn summarized to keep this conversation within budget.]",
                additional_kwargs={COMPACTION_MARKER_KEY: True},
            ),
            HumanMessage(content="question 2?"),
            AIMessage(content="answer 2."),
        ],
        monkeypatch,
    )
    assert result == [
        {"index": 0, "role": "user", "text": "question 1?"},
        {"index": 1, "role": "assistant", "text": "answer 1."},
        {
            "index": 2,
            "role": "system",
            "text": "[1 earlier turn summarized to keep this conversation within budget.]",
        },
        {"index": 3, "role": "user", "text": "question 2?"},
        {"index": 4, "role": "assistant", "text": "answer 2."},
    ]


# --- A page of the transcript (PRODUCTION_REVIEW.md R27) -----------------------------------------------------------------


def _long_conversation(turns=30):
    messages = [SystemMessage(content="You are a helpful assistant.")]
    for i in range(turns):
        messages += [
            HumanMessage(content=f"question {i}"),
            AIMessage(content="", tool_calls=[{"name": "calculator", "args": {}, "id": f"c{i}"}]),  # a stored message nobody saw: it takes no index
            ToolMessage(content="42", tool_call_id=f"c{i}"),
            AIMessage(content=f"answer {i}"),
        ]
    return messages


async def _page(monkeypatch, **kwargs):
    async def fake_init_graph_async():
        return _FakeGraph(_long_conversation())

    monkeypatch.setattr(agent_module, "init_graph_async", fake_init_graph_async)
    return await stream_module.get_session_messages("t1", **kwargs)


async def test_with_neither_limit_nor_before_the_whole_transcript_comes_back(monkeypatch):
    page = await _page(monkeypatch)

    assert len(page) == 60 and [m["index"] for m in page] == list(range(60))


async def test_limit_returns_the_last_messages_numbered_by_their_place_in_the_whole_transcript(monkeypatch):
    page = await _page(monkeypatch, limit=4)

    assert [(m["index"], m["text"]) for m in page] == [(56, "question 28"), (57, "answer 28"), (58, "question 29"), (59, "answer 29")]


async def test_a_page_counts_what_a_person_saw_not_what_was_stored(monkeypatch):
    """The tool-calling AIMessage and the ToolMessage are filtered BEFORE the slice, so 50 means 50 bubbles."""
    page = await _page(monkeypatch, limit=50)

    assert len(page) == 50 and all(m["text"] for m in page)


async def test_before_reads_the_page_that_precedes_the_last_one_and_the_pages_tile_the_transcript(monkeypatch):
    last = await _page(monkeypatch, limit=25)
    middle = await _page(monkeypatch, limit=25, before=last[0]["index"])
    first = await _page(monkeypatch, limit=25, before=middle[0]["index"])

    assert [m["index"] for m in first + middle + last] == list(range(60)), "no message twice, none missed"


async def test_before_the_start_is_an_empty_page_and_a_cursor_past_the_end_is_the_whole_tail(monkeypatch):
    assert await _page(monkeypatch, before=0) == []
    assert [m["index"] for m in await _page(monkeypatch, limit=2, before=10_000)] == [58, 59]
