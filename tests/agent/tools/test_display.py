"""Every tool a domain offers can be told to a person (specs/025): a label, for a call that is approved an effect, and summaries that still match.

A tool added without an entry would reach an approval card as its identifier, with no effect in words.
"""
import pytest

from app.agent.tools.display import SHARED_TOOL_DISPLAY
from app.core.tool_runtime import NO_CTX_REFUSAL
from app.core.untrusted import frame_untrusted
from app.domains.ecorp.display import TOOL_DISPLAY as ECORP
from app.domains.ops.display import TOOL_DISPLAY as OPS
from app.domains.registry import DOMAINS
from app.domains.sales.display import TOOL_DISPLAY as SALES
from app.domains.shared.display import WEB_SEARCH_DISPLAY
from app.domains.support.display import TOOL_DISPLAY as SUPPORT

PLUGINS = {name: plugin for name, (_manifest, plugin) in DOMAINS.items()}


@pytest.mark.parametrize("domain", sorted(PLUGINS))
def test_every_tool_has_a_label_and_every_call_that_is_approved_has_an_effect(domain):
    plugin = PLUGINS[domain]
    display, capabilities = plugin.tool_display(), plugin.tool_capabilities()
    names = {t.name for t in plugin.tools()}

    assert not names - set(display), f"tools with no ToolDisplay: {sorted(names - set(display))}"
    undescribed = [name for name in names if capabilities.get(name, "outward") != "read_only" and display[name].describe is None]
    assert not undescribed, f"a person approves these without being told what they do: {undescribed}"
    assert all(display[name].label and "_" not in display[name].label for name in names)


@pytest.mark.parametrize("domain", sorted(PLUGINS))
def test_every_description_copes_with_arguments_that_are_missing(domain):
    """A pause happens before the tool validates its arguments, so the model may have left any of them out."""
    for entry in PLUGINS[domain].tool_display().values():
        if entry.describe is not None:
            assert entry.describe({}).text


@pytest.mark.parametrize(
    ("display", "tool", "result", "summary"),
    [
        (SHARED_TOOL_DISPLAY, "search_docs", frame_untrusted("[1] Support is open 9 to 6.\n[2] Weekends are closed."), "2 results"),
        (SHARED_TOOL_DISPLAY, "search_docs", frame_untrusted("[1] See reference [7] in the handbook."), "1 result"),
        (SHARED_TOOL_DISPLAY, "search_docs", "No relevant documents found.", "No results"),
        (WEB_SEARCH_DISPLAY, "web_search", frame_untrusted("- A\n  https://a.example\n- B\n  https://b.example"), "2 results"),
        (SUPPORT, "create_ticket", "Ticket #4821 opened (high priority): VPN down", "Ticket #4821 created"),
        (SUPPORT, "create_ticket", "Ticket #9 opened (high priority) and escalated to a human agent: urgent", "Ticket #9 created"),
        (SUPPORT, "escalate_to_human", "Ticket #3 escalated to a human agent: stuck", "Ticket #3 escalated"),
        (SUPPORT, "add_ticket_comment", "Added your follow-up to ticket #12.", "Comment added to ticket #12"),
        (OPS, "log_incident", "Incident #7 logged: disk full", "Incident #7 logged"),
        (OPS, "resolve_incident", "Incident #7 resolved: cleaned up", "Incident #7 resolved"),
        (SALES, "schedule_followup", "Follow-up #5 scheduled for 2026-12-01: call", "Follow-up scheduled for 2026-12-01"),
        (SALES, "log_lead_interaction", "Logged interaction for lead #2 (Sam, sam@example.org).", "Logged for lead #2"),
        (ECORP, "add_note", "Note 'Refunds' added to the company knowledge base.", "Added to the company knowledge base"),
    ],
)
def test_a_summary_reads_what_the_tool_really_says(display, tool, result, summary):
    assert display[tool].summarize(result) == summary


@pytest.mark.parametrize("domain", sorted(PLUGINS))
def test_a_summary_says_nothing_about_a_result_it_does_not_recognise(domain):
    for name, entry in PLUGINS[domain].tool_display().items():
        if entry.summarize is not None:
            assert entry.summarize(NO_CTX_REFUSAL) is None, name
            assert entry.summarize("Something else entirely.") is None, name
