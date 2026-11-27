"""An answer given without a step it needed is sent back (GRAPH_PATTERNS.md pattern 115). From one Ecorp session (2026-11-27): "Remember that I
prefer short, bulleted answers" got "I will" with `remember` never called, "Have a researcher..." was answered without one (and one of its three
bullets was invented), and the onboarding brief said "No team notes on record" without searching for any."""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.agent.graph.routing import graph_node_check_output

TOOLS = frozenset({"remember", "run_subagent", "search_docs", "query_employees", "use_skill"})


def _used(name: str, content: str = "ok", call_id: str = "t") -> list:
    return [AIMessage(content="", tool_calls=[{"name": name, "args": {}, "id": call_id}]), ToolMessage(content=content, tool_call_id=call_id, name=name)]


def _step(request: str, answer: str, between: list | None = None, tools: frozenset = TOOLS) -> str | None:
    messages = [HumanMessage(content=request), *(between or []), AIMessage(content=answer)]
    result = graph_node_check_output({"messages": messages, "citations": []}, system_prompt="", tool_names=tools)
    return result["output_findings"].get("skipped_step")


@pytest.mark.parametrize(
    ("request_text", "answer", "tool"),
    [
        ("Remember that I prefer short, bulleted answers.", "Understood. I will provide short, bulleted answers in the future.", "remember"),
        ("Please remember my timezone is UTC+7", "Noted, your timezone is UTC+7.", "remember"),
        ("Have a researcher pull together what our docs say about Qdrant.", "- Qdrant is a vector database.", "run_subagent"),
        ("Ask the metrics researcher how things look now.", "Things look fine from here.", "run_subagent"),
    ],
)
def test_a_requested_step_that_never_ran_is_caught(request_text, answer, tool):
    assert _step(request_text, answer) == tool


@pytest.mark.parametrize(
    ("request_text", "answer", "between"),
    [
        ("Remember that I prefer bullets.", "- Saved.", _used("remember")),
        # Declined at the approval card: the call is in the turn, so the answer that says so stands.
        ("Remember that I prefer bullets.", "Okay, I left it.", _used("remember", "The user rejected this action.")),
        ("remember our refund policy", "Added a note.", _used("add_note")),  # saved, in the knowledge base
        ("Remember my card number is 4111.", "I can't store payment details.", None),
        ("Do you remember what I asked first?", "You asked about checkpointers.", None),
        ("Remember what I said about Qdrant and sum it up.", "You said it stores vectors.", None),
        ("Who is the researcher on this team?", "Nobody has that title.", None),
        ("Have a researcher look into Qdrant.", "- It stores vectors.", _used("run_subagent", "findings")),
    ],
)
def test_a_step_that_ran_or_was_not_asked_for_is_not(request_text, answer, between):
    assert _step(request_text, answer, between) is None


def test_a_domain_without_the_tool_is_never_told_to_call_it():
    assert _step("Remember that I prefer bullets.", "I will.", tools=frozenset({"search_docs"})) is None


SKILL = _used("use_skill", "2. Call `search_docs` with `topic=company` ... If it returned nothing, write \"No team notes on record\".", "s")
NO_NOTES = "**Role**: Staff Engineer. **Team & background**: No team notes on record."


def test_saying_nothing_is_on_record_without_the_search_the_skill_asked_for_is_caught():
    assert _step("Prepare an onboarding brief for Priya Nair.", NO_NOTES, SKILL + _used("query_employees", "- Priya Nair", "q")) == "search_docs"


@pytest.mark.parametrize(
    ("answer", "between"),
    [
        (NO_NOTES, SKILL + _used("search_docs", "No relevant documents found.", "d")),  # searched: the claim is true
        (NO_NOTES, None),  # no skill asked for a search
        ("No open tickets found for this customer.", SKILL),  # about another lookup
    ],
)
def test_an_honest_or_unrelated_nothing_found_is_not(answer, between):
    assert _step("Prepare an onboarding brief for Priya Nair.", answer, between) is None


def test_the_person_is_told_a_step_did_not_run_when_retries_are_spent():
    from app.agent.graph.output_checks import check_for

    check = check_for("skipped_step")

    assert "remember" in check.feedback("remember")
    assert "no notes were found" in check.feedback("search_docs") and "notes" not in check.feedback("remember")
    assert check.trust_on_exhaust is False  # "I will remember that" must not be shown when nothing was saved
    assert "did not run" in check.fallback


@pytest.mark.parametrize(
    ("held", "caught"),
    [
        ("I prefer short, bulleted answers.", None),  # already saved: "Got it" is true, and a second copy is not wanted
        ("My manager is Marcus Cole.", "remember"),
    ],
)
def test_remembering_what_a_recalled_memory_already_says_needs_no_second_save(held, caught):
    """Found while testing the check: the second "Remember that I prefer..." found the first one in its context, rightly saved nothing, and was
    told "I didn't do that"."""
    memory = {"marker": "[1]", "title": "memory", "text": held}
    messages = [HumanMessage(content="Remember that I prefer short, bulleted answers."), AIMessage(content="Got it, short bulleted answers [1].")]

    result = graph_node_check_output({"messages": messages, "citations": [memory]}, system_prompt="", tool_names=TOOLS)

    assert result["output_findings"].get("skipped_step") == caught
