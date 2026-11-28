"""A `[n]` that points at nothing, or at a source that cannot support it, is removed (GRAPH_PATTERNS.md pattern 104). The answers are from one
Ecorp session (2026-11-27): the first `[1]` of the conversation was real, and qwen2.5:7b kept writing it on answers that had no source."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph.citations import find_likely_misattributed
from app.agent.graph.routing import graph_node_check_output

CHECKPOINTER = {"marker": "[1]", "text": "A checkpointer persists a snapshot of the graph's state at every super-step, grouped into threads.", "score": 0.2}


def _judge(answer: str, *, question: str = "q", citations: list | None = None, between: list | None = None, **kwargs):
    messages = [HumanMessage(content=question), *(between or []), AIMessage(content=answer)]
    result = graph_node_check_output({"messages": messages, "citations": citations or []}, system_prompt="", **kwargs)
    shown = result["messages"][0].content if "messages" in result else answer
    return shown, result


@pytest.mark.parametrize(
    ("answer", "shown"),
    [
        ("Total Expenses: $267.70 [1]\n\n[1] Calculated as 23.50 + 180 + 64.20 = 267.70", "Total Expenses: $267.70\n\nCalculated as 23.50 + 180 + 64.20 = 267.70"),
        ("**Team & background**: No team notes on record. [1]", "**Team & background**: No team notes on record."),
        ("Priya is a Staff Engineer [1][2].", "Priya is a Staff Engineer."),
    ],
)
def test_a_marker_with_no_source_at_all_is_removed(answer, shown):
    text, result = _judge(answer)

    assert text == shown
    assert result["used_citations"] == []


@pytest.mark.parametrize(
    "answer",
    [
        "The second item is items[1] in that list.",  # an index, not a citation
        "Run `echo $a [1]` to see it.",  # code
        "See the note [1](https://example.com/a) for details.",  # a link
    ],
)
def test_brackets_that_are_not_citations_are_left_alone(answer):
    assert _judge(answer)[0] == answer


def test_a_number_the_person_wrote_in_brackets_is_theirs():
    answer = "The sentence reads: growth was strong [1]."

    assert _judge(answer, question="Repeat this: growth was strong [1].")[0] == answer


def test_a_marker_for_a_document_a_tool_returned_is_kept():
    """`search_docs` numbers its hits in its own result, which is not in `citations`."""
    result = ToolMessage(content="[1] Support is free for open-source users.", tool_call_id="t", name="search_docs")
    call = AIMessage(content="", tool_calls=[{"name": "search_docs", "args": {"query": "support"}, "id": "t"}])

    assert _judge("Support is free for open-source users [1].", between=[call, result])[0].endswith("[1].")


def test_a_subagents_answer_is_not_touched():
    assert _judge("Three vendors matched [1].", enforce_citations=False)[0] == "Three vendors matched [1]."


def test_a_short_figure_the_source_never_states_loses_its_marker():
    """Too short to judge by its words, so it kept pointing at a page about checkpointers."""
    text, result = _judge("The result of 15% of 2,450 is 367.5 [1].", citations=[CHECKPOINTER])

    assert text == "The result of 15% of 2,450 is 367.5."
    assert result["used_citations"] == []


def test_a_bare_figure_with_no_words_at_all_is_judged_too():
    assert find_likely_misattributed("367.5 [1].", [CHECKPOINTER], [CHECKPOINTER]) == [CHECKPOINTER]


@pytest.mark.parametrize("sentence", ["Founded in 2021 [1].", "Yes, super-step 3 [1].", "Threads, mostly [1]."])
def test_a_short_sentence_that_the_source_could_support_keeps_its_marker(sentence):
    source = {"marker": "[1]", "text": "Ecorp was founded in 2021. Snapshots are grouped into threads at every super-step."}

    assert find_likely_misattributed(sentence, [source], [source]) == []


TOOLS = frozenset({"search_docs", "calculator"})


@pytest.mark.parametrize(
    ("answer", "shown"),
    [
        ("No team notes on record [search_docs tool response].", "No team notes on record."),
        ("Total: $267.70 [calculator]", "Total: $267.70"),
    ],
)
def test_a_citation_of_one_of_the_domains_own_tools_is_removed(answer, shown):
    """`qwen2.5:7b`, sent back to make a search, then cited it by the tool's name."""
    assert _judge(answer, tool_names=TOOLS)[0] == shown


@pytest.mark.parametrize("answer", ["Run `[search_docs]` as written.", "See [search](https://example.com).", "The [calculator](https://example.com) page."])
def test_other_brackets_around_a_tool_name_stay(answer):
    assert _judge(answer, tool_names=TOOLS)[0] == answer
