"""A tool call's id is a database key (`idempotent()`'s claim and UNIQUE columns), so the agent bounds it as the model's reply enters State
(GRAPH_PATTERNS.md pattern 111). LiteLLM packs Gemini's thought signature into the id: 4,000 to 6,000 characters on a hard question, and
Postgres refused the insert: "index row size 4816 exceeds btree version 4 maximum 2704"."""
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langgraph.graph.message import add_messages

from app.agent.graph.agent_node import make_graph_node_agent
from app.agent.graph.messages import MAX_TOOL_CALL_ID_BYTES, with_bounded_tool_call_ids

LONG = "call_aa__thought__" + "A" * 5000
OTHER = "call_bb__thought__" + "A" * 5000


def _calls(*ids):
    return [{"name": "calculator", "args": {"expression": "1+1"}, "id": i, "type": "tool_call"} for i in ids]


def test_a_long_id_is_replaced_by_a_short_one_that_stays_distinct():
    message, shortened = with_bounded_tool_call_ids(AIMessage(content="", tool_calls=_calls(LONG, OTHER)))

    first, second = (call["id"] for call in message.tool_calls)
    assert shortened == 2
    assert first != second
    assert max(len(first), len(second)) <= MAX_TOOL_CALL_ID_BYTES


def test_a_message_whose_ids_fit_is_left_alone():
    """A short signature is worth keeping: the model gets it back on the next call."""
    original = AIMessage(content="", tool_calls=_calls("call_cc__thought__" + "A" * 800))

    message, shortened = with_bounded_tool_call_ids(original)

    assert message is original and shortened == 0


def test_a_streamed_reply_keeps_the_short_id_when_it_is_stored():
    """A streamed reply carries each id twice, and storing it rebuilds `tool_calls` from the chunks."""
    chunk = AIMessageChunk(content="", tool_call_chunks=[{"name": "calculator", "args": "{}", "id": LONG, "index": 0, "type": "tool_call_chunk"}])

    message, _ = with_bounded_tool_call_ids(chunk)
    (stored,) = add_messages([], [message])

    assert stored.tool_calls[0]["id"] == message.tool_calls[0]["id"] != LONG
    assert AIMessageChunk(**message.model_dump()).tool_calls[0]["id"] != LONG


async def test_the_agent_node_never_puts_a_long_id_into_the_state():
    llm = FakeMessagesListChatModel(responses=[AIMessage(content="", tool_calls=_calls(LONG))])

    update = await make_graph_node_agent(llm, system_prompt="s")({"messages": [HumanMessage(content="add one and one")]})

    assert len(update["messages"][0].tool_calls[0]["id"]) <= MAX_TOOL_CALL_ID_BYTES
