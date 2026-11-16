"""Output checks are counted by the model tier that wrote the answer, and a deployment may switch some off on the routed tiers (PRODUCTION_REVIEW.md R32).

Many checks exist because a 3B or 7B local model drops instructions (a missing citation, "I will do X" instead of doing it). Each is cheap; a false
positive is not, because it costs another full model round, and against a strong model most fire rarely or never. Nothing recorded which fire for which
tier, so nobody could say which still earn their place once `ROUTER_MODE=on` sends turns to the medium or strong tier.
"""
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.graph import build as graph_build
from app.agent.graph.build import GraphDeps
from app.agent.graph.output_checks import CHECKS, parse_checks_off_on_routed_tiers
from app.agent.graph.routing import check_output
from app.core import metrics
from tests.conftest import metric_value
from tests.ecorp import ECORP_SYSTEM_PROMPT, build_graph

TOO_SHORT = "No."  # under MIN_ANSWER_LENGTH: the `too_short` check fires


def _state(tier=None, answer=TOO_SHORT):
    state = {"messages": [HumanMessage(content="what is the refund policy?"), AIMessage(content=answer)], "citations": []}
    if tier is not None:
        state["model_route"] = tier
    return state


def _fired(check, tier):
    return metric_value(metrics.agent_output_check_fired_total, check=check, tier=tier)


@pytest.mark.parametrize(("route", "tier"), [(None, "local"), ("local", "local"), ("medium", "medium"), ("strong", "strong")])
def test_every_finding_is_counted_under_the_tier_that_wrote_the_answer(route, tier):
    before = _fired("too_short", tier)

    result = check_output(_state(route), ECORP_SYSTEM_PROMPT)

    assert "too_short" in result["output_findings"]
    assert _fired("too_short", tier) == before + 1


def test_the_existing_per_check_counters_still_count_because_alerts_and_dashboards_read_them():
    before = metric_value(metrics.agent_deferred_instead_of_acting_total)

    result = check_output(_state("strong", "I will use the search_docs tool to look up the refund policy for you."), ECORP_SYSTEM_PROMPT)

    assert "deferred" in result["output_findings"]
    assert metric_value(metrics.agent_deferred_instead_of_acting_total) == before + 1


def test_a_listed_check_does_not_fire_on_a_strong_or_medium_turn():
    for tier in ("medium", "strong"):
        before = _fired("too_short", tier)

        result = check_output(_state(tier), ECORP_SYSTEM_PROMPT, skip_on_routed_tiers=frozenset({"too_short"}))

        assert "too_short" not in result["output_findings"] and result["last_retry_reason"] is None
        assert _fired("too_short", tier) == before, "a check that did not run is not counted as having fired"


def test_a_listed_check_still_fires_on_the_local_tier_whether_the_router_is_off_or_kept_it_local():
    for route in (None, "local"):
        result = check_output(_state(route), ECORP_SYSTEM_PROMPT, skip_on_routed_tiers=frozenset({"too_short"}))

        assert "too_short" in result["output_findings"]


def test_a_check_that_is_not_listed_still_fires_on_a_strong_turn():
    result = check_output(_state("strong"), ECORP_SYSTEM_PROMPT, skip_on_routed_tiers=frozenset({"uncited"}))

    assert "too_short" in result["output_findings"]


class TestTheSetting:
    def test_empty_means_every_check_on_every_tier(self):
        assert parse_checks_off_on_routed_tiers("") == frozenset()
        assert parse_checks_off_on_routed_tiers(" , ,") == frozenset()

    def test_codes_are_split_on_commas_and_trimmed(self):
        assert parse_checks_off_on_routed_tiers("too_short, uncited ,deferred") == frozenset({"too_short", "uncited", "deferred"})

    def test_an_unknown_code_is_refused_without_echoing_what_was_typed(self):
        with pytest.raises(ValueError) as refused:
            parse_checks_off_on_routed_tiers("too_shrt")

        message = str(refused.value)
        assert "OUTPUT_CHECKS_OFF_ON_ROUTED_TIERS" in message
        assert "too_shrt" not in message, "names the variable and the vocabulary, never the value (pattern 59)"
        assert all(check.code in message for check in CHECKS), "it lists what the codes are, so the typo can be fixed"

    @pytest.mark.parametrize("code", ["leaked_prompt", "fabricated"])
    def test_the_two_checks_that_protect_the_person_cannot_be_switched_off(self, code):
        with pytest.raises(ValueError, match="protect the person"):
            parse_checks_off_on_routed_tiers(f"too_short,{code}")

    def test_every_check_not_protected_can_be_listed(self):
        assert parse_checks_off_on_routed_tiers(",".join(c.code for c in CHECKS if c.code not in {"leaked_prompt", "fabricated"}))

    @pytest.mark.parametrize("bad", ["not_a_check", "leaked_prompt"])
    def test_the_graph_refuses_to_build_with_a_bad_list_so_the_process_stops_at_start_up(self, monkeypatch, bad):
        monkeypatch.setattr(graph_build, "OUTPUT_CHECKS_OFF_ON_ROUTED_TIERS", bad)

        with pytest.raises(ValueError, match="OUTPUT_CHECKS_OFF_ON_ROUTED_TIERS"):
            build_graph(GraphDeps(llm=GenericFakeChatModel(messages=iter([]))))

    def test_a_valid_list_builds(self, monkeypatch):
        monkeypatch.setattr(graph_build, "OUTPUT_CHECKS_OFF_ON_ROUTED_TIERS", "too_short,uncited")

        assert build_graph(GraphDeps(llm=GenericFakeChatModel(messages=iter([]))))
