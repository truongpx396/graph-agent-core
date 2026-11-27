"""The web client's "Try asking" entries say which skill, subagents and tools a question exercises (web/src/features/workspace/suggestions.json). A person
tests the agent against those marks, so a renamed skill or tool must not leave a mark naming something the domain no longer has."""
import json
from pathlib import Path

from app.agent.tools.skill_catalog import load_skills
from app.agent.tools.subagent_catalog import load_subagents
from app.domains.registry import DOMAINS

SUGGESTIONS = json.loads((Path(__file__).resolve().parents[2] / "web/src/features/workspace/suggestions.json").read_text(encoding="utf-8"))


def unknown_names(suggestions: dict, skills: dict, subagents: dict, tools: dict) -> list[tuple[str, str]]:
    """`(domain, name)` for each skill, subagent or tool a domain's entries name and the domain does not have."""
    found = []
    for domain, entries in suggestions.items():
        for entry in entries:
            found += [(domain, name) for name in [entry.get("skill")] if name and domain not in (skills.get(name) or ())]
            found += [(domain, name) for name in entry.get("subagents", []) if domain not in (subagents.get(name) or ())]
            found += [(domain, name) for name in entry.get("tools", []) if name not in tools[domain]]
    return found


def _tools_of(domain: str) -> set[str]:
    manifest, plugin = DOMAINS[domain]
    return set(manifest.allowed_tools) or {tool.name for tool in plugin.tools()}


def test_every_mark_names_something_its_domain_has():
    skills = {name: record.domains for name, record in load_skills().items()}
    subagents = {name: record.domains for name, record in load_subagents().items()}

    assert unknown_names(SUGGESTIONS, skills, subagents, {domain: _tools_of(domain) for domain in DOMAINS}) == []


def test_every_domain_has_questions_and_none_is_asked_twice():
    assert set(SUGGESTIONS) == set(DOMAINS)
    for domain, entries in SUGGESTIONS.items():
        texts = [entry["text"] for entry in entries]
        assert texts and len(texts) == len(set(texts)), domain


def test_the_check_can_fail():
    suggestions = {"ops": [{"text": "q", "skill": "gone", "subagents": ["other-domains"], "tools": ["fetch", "renamed"]}]}

    found = unknown_names(suggestions, {}, {"other-domains": ("sales",)}, {"ops": {"fetch"}})

    assert found == [("ops", "gone"), ("ops", "other-domains"), ("ops", "renamed")]
