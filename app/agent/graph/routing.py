"""The two conditional-edge gate functions — `graph_edge_route_after_agent` (after `agent`: tool call, final
answer, or safety budget?) and `check_output` (is the final answer good enough to end on?) — plus
`graph_edge_route_after_check_output`, which routes on check_output's result.

Their helpers: `graph/loop_guards.py` (tool-call fingerprinting and repeat detection, the
mandatory-approval gate), `graph/tool_guards.py` (`batch_problem`, the malformed-batch check),
`graph/output_checks.py` (the registry `check_output` runs) with `graph/citations.py` and
`graph/output_guardrails.py` (the heuristics behind its checks).
"""
from collections.abc import Mapping
from typing import Any, Literal, cast

from langchain_core.messages import AIMessage
from langgraph.prebuilt import tools_condition

from app.agent.graph.citations import (
    count_ungrounded_claims,
    drop_all_citation_markers,
    drop_citation_markers,
    find_likely_misattributed,
    find_likely_uncited,
    find_used_citations,
    insert_missing_citation_markers,
)
from app.agent.graph.loop_guards import (
    consecutive_repeat_count,
    mandatory_gate_reason,
)
from app.agent.graph.messages import (
    GATEWAY_REFUSAL_KEY,
    context_gap_original,
    tool_ran_this_turn,
)
from app.agent.graph.output_checks import ROUTED_TIERS, CheckContext, run_checks
from app.agent.graph.output_guardrails import (
    strip_closing_offer,
    strip_fabricated_reference_footer,
)
from app.agent.graph.state import (
    MAX_CONSECUTIVE_SAME_RETRY_REASON,
    MAX_ITERATIONS,
    MAX_REPEATED_ACTIONS,
    MAX_TOKENS_PER_TURN,
    State,
)
from app.agent.graph.tool_guards import batch_problem
from app.core import metrics
from app.core.config import MAX_COST_USD_PER_TURN


# --- Edge fn: after agent, route to tools / output check / abort ---
def graph_edge_route_after_agent(
    state: State,
    tool_capabilities: Mapping[str, str],
    valid_tool_names: frozenset[str],
    max_iterations: int = MAX_ITERATIONS,
    max_tokens: int = MAX_TOKENS_PER_TURN,
    max_cost_usd: float = MAX_COST_USD_PER_TURN,
) -> Literal[
    "tools",
    "human_approval",
    "tool_batch_guard",
    "check_output",
    "no_answer",
]:
    """Did the LLM call a tool, give a final answer, or hit a safety budget?

    Checked in order: iteration/token/cost caps (turn-wide safety nets, first whatever the LLM said) -> is
    this a tool call -> a malformed batch (`batch_problem`: too many calls, a name that isn't one of this
    domain's tools, or `use_skill` before `skill_search`) -> human approval. The batch check runs BEFORE
    human_approval on purpose: none of those is a real call a human could meaningfully approve, so all go to
    `tool_batch_guard`, which answers every call and loops back to `agent` for a self-correcting retry instead
    of surfacing garbage for review.

    Two independent reasons route to `human_approval`, one optional:
    - `require_approval` on state: opt-in (default False), see `chat.py`'s `--hitl` mode.
    - Any pending tool_call whose declared capability (`tool_capabilities`, the calling domain's mapping)
      isn't "read_only": mandatory, never skippable via `require_approval=False`. An *undeclared* tool counts
      as "outward", so forgetting to register one fails toward caution.

    `tool_capabilities`/`valid_tool_names` are the calling domain's; `build_graph` binds them with
    `functools.partial` (pattern 23). There is no shared default: a registered tool and its tier only mean
    something relative to one domain. `max_iterations`/`max_tokens`/`max_cost_usd` default to this module's
    constants so a NESTED subagent run (pattern 46) can bind its own smaller MAX_SUBAGENT_* ceiling the same
    way. It stays a plain module-level function so it is callable with just `state`, like every routing
    function here.

    All four safety-net exits route to `"no_answer"`, not `END`: `no_answer_fallback` turns "a budget fired
    before check_output ran" into a user-visible message instead of ending on `agent`'s last, often empty,
    AIMessage (a model that produces no tool call or content still burns tokens). It is the top-level twin of
    the empty-final-AIMessage signal `run_subagent` uses for a nested run.
    """
    # The gateway's content guardrail refused the call (`agent` wrote the refusal): that IS the
    # turn's answer. Routed to `no_answer`, which ends the turn without a retry, follow-ups or a
    # cache write, so a refusal is never stored as the answer to this question.
    last = state["messages"][-1] if state.get("messages") else None
    if isinstance(last, AIMessage) and last.additional_kwargs.get(GATEWAY_REFUSAL_KEY):
        return "no_answer"
    if state.get("iterations", 0) >= max_iterations:
        return "no_answer"
    # Folds any run_subagent spend THIS turn into the parent's own live
    # ceiling (pattern 46's disclosed gap) — each nested run is still
    # separately bounded by its own MAX_SUBAGENT_* ceiling.
    subagent_spend = state.get("subagent_spend", [])
    effective_tokens = state.get("total_tokens", 0) + sum(t for t, _ in subagent_spend)
    if effective_tokens >= max_tokens:
        metrics.agent_token_budget_exceeded_total.inc()
        return "no_answer"
    effective_cost_usd = state.get("total_cost_usd", 0.0) + sum(c for _, c in subagent_spend)
    if effective_cost_usd >= max_cost_usd:
        # A HARD stop (pattern 35), independent of the token cap above —
        # the same token count costs differently across model tiers, so $
        # is its own budget, not a derived quantity of tokens.
        metrics.agent_cost_ceiling_exceeded_total.inc()
        return "no_answer"
    result = tools_condition(state)  # type: ignore[arg-type]  # State is a valid Mapping at runtime; tools_condition's stub just doesn't say so
    if result != "tools":
        return "check_output"
    tool_calls = cast(AIMessage, state["messages"][-1]).tool_calls or []
    if batch_problem(tool_calls, state["messages"], valid_tool_names):
        return "tool_batch_guard"
    if consecutive_repeat_count(state["messages"]) >= MAX_REPEATED_ACTIONS:
        # Checked independently of MAX_ITERATIONS — a run looping on one
        # identical action would otherwise just exhaust the iteration cap,
        # indistinguishable from one that's actually converging (pattern 34).
        metrics.agent_no_progress_total.inc()
        return "no_answer"
    mandatory_reason = mandatory_gate_reason(tool_calls, tool_capabilities)
    if mandatory_reason:
        metrics.agent_capability_gate_total.labels(capability=mandatory_reason).inc()
    if state.get("require_approval") or mandatory_reason:
        return "human_approval"
    return "tools"


# --- Node: check output — also extracts which offered citations were
# actually used (find_used_citations) and how many cited markers were invented
# (count_ungrounded_claims). Recomputed from scratch every call, so a
# retry_output round doesn't leave stale values from the rejected answer.
#
# `system_prompt` is the calling domain's own, bound by build_graph via
# functools.partial, so `reveals_system_prompt` checks an answer against the prompt
# it was ACTUALLY seeded with. ---
def graph_node_check_output(
    state: State,
    system_prompt: str,
    *,
    enforce_citations: bool = True,
    skill_required_tools: tuple[str, ...] = (),
    tool_names: frozenset[str] = frozenset(),
    skip_on_routed_tiers: frozenset[str] = frozenset(),
    checks_off: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Judge the final answer: strip a fabricated reference footer, cut a closing offer off a complete answer,
    repair a missing citation marker and drop an unsupported one, then run every check in `graph/output_checks.py`'s registry. Returns
    `used_citations`/`ungrounded_claims_count` for the answer as shown, `output_findings` (`{code: detail}` of
    every check that fired), `last_retry_reason` (the first by priority, or None),
    `retry_reason_repeat_count`, and a corrected `messages` entry when a footer or offer was stripped or a
    marker inserted.

    `system_prompt`, `enforce_citations` and `skill_required_tools` are the calling domain's, bound by
    `build_graph` via `functools.partial`. `enforce_citations=False` (a nested subagent run) turns both
    citation checks off and inserts no `[n]` marker: a subagent is told NOT to use bracket markers (they'd
    name its own sources, meaningless to the parent), so judging or "repairing" by them contradicted its
    prompt and put a stray `[1]` in the text handed back. Every other check still applies.
    `skill_required_tools` is the subset of `SKILL_REQUIRED_TOOL_MARKERS` this domain has. `tool_names` are its bound tools (a call to one printed
    as text is rejected, pattern 109).
    `skip_on_routed_tiers` (`OUTPUT_CHECKS_OFF_ON_ROUTED_TIERS`, validated at build) are checks that do not run when the turn was routed to the `medium`
    or `strong` tier: they exist to correct small local models, and a false positive costs a full retry round. The `local` tier is unchanged, and every
    finding is counted by tier (`agent_output_check_fired_total`) so the list can be chosen from numbers. `checks_off` do not run on any tier
    (`CONTEXT_GAP_REASK_ENABLED=false`).

    The second look (`context_gap`, pattern 105) has two rules of its own. If it looked nothing up, whatever it says came from the model's memory, so the
    answer it replaced is put back: the re-ask exists to give a tool a chance, and must never end worse than not asking. And either way it has no source
    list, so no `[n]` marker is shown.
    """
    tier = state.get("model_route") or "local"
    last = state["messages"][-1]
    content = getattr(last, "content", "") or ""
    citations = state.get("citations") or []

    taken_back = context_gap_original(state["messages"])
    # A gateway refusal is the turn's answer whatever came before it (`no_answer` judges it through here), so it is never restored over.
    if taken_back is not None and not tool_ran_this_turn(state["messages"]) and not last.additional_kwargs.get(GATEWAY_REFUSAL_KEY):
        # Measured: the second look without a tool is where the invented answers came from ("The CEO of Ecorp is John Doe", generic steps for a new
        # laptop). The first answer already passed every check ahead of this one, so it is final.
        metrics.agent_context_gap_reask_total.labels(outcome="restored").inc()
        return {
            "messages": [last.model_copy(update={"content": drop_all_citation_markers(str(taken_back.content))})],
            "used_citations": [],
            "ungrounded_claims_count": 0,
            "output_findings": {},
            "last_retry_reason": None,
            "retry_reason_repeat_count": 0,
        }

    # Strip a fabricated reference-list footer FIRST, before any citation
    # computation — it isn't a real answer sentence to judge grounding on,
    # and every field below should reflect the cleaned text.
    message_update: dict[str, Any] = {}
    cleaned_content = strip_fabricated_reference_footer(content)
    if cleaned_content != content:
        metrics.agent_reference_footer_stripped_total.inc()
        content = cleaned_content
        message_update["messages"] = [last.model_copy(update={"content": content})]

    # A closing offer after a complete answer is cut, not sent back: asking the model to rewrite
    # it does not work (strip_closing_offer's docstring has the trace). `content` stays what the
    # model wrote, for the checks that must see all of it; `body` is what the rest is judged on.
    body = strip_closing_offer(content)
    if body != content:
        metrics.agent_closing_offer_stripped_total.inc()
        message_update["messages"] = [last.model_copy(update={"content": body})]

    used = find_used_citations(body, citations)
    if citations and not used and body:
        # Directional signal only, not enforced — a legitimate
        # general-knowledge/calculator-only answer looks IDENTICAL to a
        # silently dropped mandatory citation (retrieve_context always
        # returns its top-K docs regardless of relevance, and the prompt
        # allows citing nothing for either case). graph_edge_route_after_check_output
        # deliberately doesn't retry on this.
        metrics.agent_zero_citations_total.inc()

    # Auto-correct rather than retry, in two steps whose ORDER matters. `shown`/`shown_used` are what the user will actually see, so every returned field
    # reflects that; both steps reuse `message_update`, so they compose with the footer strip and the offer cut onto the same final message.
    #
    # 1. Drop a marker that sits only on sentences sharing nothing with its source (pattern 104). It used to send the answer back ("that source does not
    #    support what you wrote"): a small model returned the same sentence with the same marker, `retry_exhausted` then replaced a correct "I could not find
    #    that" with a failure message, and the one retry that converged did so by deleting content. The check knows exactly which markers are wrong, so
    #    there is nothing for the model to decide.
    shown, shown_used = body, used
    if enforce_citations:
        misattributed = find_likely_misattributed(shown, citations, shown_used)
        if misattributed:
            metrics.agent_misattributed_citations_total.inc()
            metrics.agent_output_check_fired_total.labels(check="misattributed", tier=tier).inc()
            shown = drop_citation_markers(shown, [c["marker"] for c in misattributed])
            shown_used = find_used_citations(shown, citations)
            message_update["messages"] = [last.model_copy(update={"content": shown})]

    # 2. Insert a missing marker (insert_missing_citation_markers's docstring has the evidence: 7 different prompt-level attempts, all failed). After step 1,
    #    so a marker that sat on the wrong sentence of an answer whose RIGHT sentence had none moves, instead of leaving that sentence "uncited" and sending
    #    the answer back for it (a misplaced marker is the common shape of a misattribution).
    if enforce_citations and find_likely_uncited(shown, citations, shown_used):
        corrected_content, fixed = insert_missing_citation_markers(shown, citations, shown_used)
        if fixed:
            metrics.agent_citation_auto_inserted_total.inc()
            shown = corrected_content
            shown_used = find_used_citations(shown, citations)
            message_update["messages"] = [last.model_copy(update={"content": shown})]

    if taken_back is not None and drop_all_citation_markers(shown) != shown:
        shown, shown_used = drop_all_citation_markers(shown), []
        message_update["messages"] = [last.model_copy(update={"content": shown})]

    findings = run_checks(
        CheckContext(
            content=content,
            body=body,
            messages=state["messages"],
            citations=citations,
            used=used,
            shown=shown,
            shown_used=shown_used,
            system_prompt=system_prompt,
            enforce_citations=enforce_citations,
            skill_required_tools=skill_required_tools,
            tool_names=tool_names,
        ),
        skip=checks_off | (skip_on_routed_tiers if tier in ROUTED_TIERS else frozenset()),
    )
    for code in findings:
        metrics.agent_output_check_fired_total.labels(check=code, tier=tier).inc()
    reason = next(iter(findings), None)
    if taken_back is not None and reason is None:
        metrics.agent_context_gap_reask_total.labels(outcome="looked_up").inc()
    prior_reason = state.get("last_retry_reason")
    if reason is None:
        repeat_count = 0
    elif reason == prior_reason:
        repeat_count = (state.get("retry_reason_repeat_count") or 0) + 1
    else:
        repeat_count = 1
    return {
        **message_update,
        "used_citations": shown_used,
        "ungrounded_claims_count": count_ungrounded_claims(shown, citations),
        "output_findings": findings,
        "last_retry_reason": reason,
        "retry_reason_repeat_count": repeat_count,
    }


def graph_edge_route_after_check_output(
    state: State,
) -> Literal["retry_output", "retry_exhausted", "finish"]:
    """Retry, give up, or `"finish"`: the answer stands, and `build_graph` fans out to whichever
    post-answer steps the graph has (follow-up suggestions, the cache write-through). Those do not
    depend on each other, so they run in parallel rather than one after the other."""
    reason = state.get("last_retry_reason")
    if reason is None:
        return "finish"
    if (state.get("retry_reason_repeat_count") or 0) >= MAX_CONSECUTIVE_SAME_RETRY_REASON:
        # The SAME rejection reason fired on consecutive rounds — the
        # model isn't converging (see MAX_CONSECUTIVE_SAME_RETRY_REASON),
        # so another round would just spend a call for a predictable outcome.
        metrics.agent_retry_exhausted_total.inc()
        return "retry_exhausted"
    return "retry_output"
