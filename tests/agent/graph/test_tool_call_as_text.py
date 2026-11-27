"""An answer that prints a call to one of the domain's tools ran nothing, so it is sent back (GRAPH_PATTERNS.md pattern 109)."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph.output_guardrails import writes_tool_call_as_text
from app.agent.graph.routing import graph_node_check_output

TOOLS = frozenset({"log_lead_interaction", "list_recent_incidents", "calculator"})
# The two shapes qwen2.5:7b actually produced.
PRINTED_CALLS = [
    'log_lead_interaction({"contact": "jordan@example.com", "name": "Jordan"})',
    'Let me check.\n\n```json\n{\n    "name": "list_recent_incidents",\n    "arguments": {}\n}\n```',
]


@pytest.mark.parametrize("answer", PRINTED_CALLS)
def test_a_printed_call_is_rejected(answer):
    result = graph_node_check_output(
        {"messages": [HumanMessage(content="do it"), AIMessage(content=answer)], "citations": []}, system_prompt="", tool_names=TOOLS
    )

    assert result["last_retry_reason"] == "call_as_text"
    assert result["output_findings"]["call_as_text"] in TOOLS  # the feedback names it


@pytest.mark.parametrize(
    "answer",
    [
        "I used list_recent_incidents to look: nothing matches.",
        "The calculator (a plain arithmetic tool) cannot hold a loop.",
        'A package.json needs a "name": "calculator" entry and a version.',
        'send_email({"to": "a@example.com"}) is not one of this domain\'s tools.',
    ],
)
def test_naming_a_tool_is_not_calling_it(answer):
    assert not writes_tool_call_as_text(answer, TOOLS)
