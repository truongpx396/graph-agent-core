"""`scripts/tools/ops_investigate.py` leaves the system prompt to the graph (pattern 19).

It seeded its own copy into the thread, from before the agent node added the prompt on every call: the model was then sent it twice, and the
stored copy was the domain's text without the platform's rules.
"""
import asyncio

from langchain_core.messages import AIMessage, SystemMessage

from scripts.tools import ops_investigate


def test_the_question_is_all_it_puts_in_the_thread(monkeypatch):
    seen: dict = {}

    class _Graph:
        async def ainvoke(self, state, config):
            seen.update(state)
            return {"messages": [AIMessage(content="nothing unusual")]}

    monkeypatch.setattr(ops_investigate, "build_graph", lambda **_kwargs: _Graph())

    answer = asyncio.run(ops_investigate.investigate("anything odd?"))

    assert answer == "nothing unusual"
    assert [m.content for m in seen["messages"]] == ["anything odd?"]
    assert not any(isinstance(m, SystemMessage) for m in seen["messages"])
