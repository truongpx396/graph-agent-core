"""What the model reads (prompts, tool descriptions, argument descriptions) names only tools it has, and holds nothing written for a developer.

Two bugs, both invisible when reading the source file by file. The ops and sales prompts talked about `calculator`, which neither has, and
the shared `search_docs` description sent support and sales to `query_employees`, which only Ecorp has: a model that reads a tool's name
may call it. And a tool's docstring IS its description, so `ask_clarification` sent every domain "Deliberately NOT special-cased in the
graph (GRAPH_PATTERNS.md pattern 27) ... the SYSTEM_PROMPT instructs the model", and others sent file paths and TOOL_CAPABILITIES.
Developer notes belong in a comment. (Skills have their own tool-name test beside this one.)
"""
import json
import re

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from app.agent.tools.skill_catalog import load_skills
from app.agent.tools.subagent_catalog import get_subagents
from app.core.prompt_rules import compose_system_prompt
from app.domains.registry import DOMAINS

DEVELOPER_NOTE = re.compile(r"_PATTERNS|\bpattern \d|SYSTEM_PROMPT|TOOL_CAPABILITIES|\bapp/|\w\.py\b|(?:AGENT|SKILL|CLAUDE)\.md|graph_(?:node|edge)_")


def _tool_names(domain: str) -> set[str]:
    return {tool.name for tool in DOMAINS[domain][1].tools()}


# Names that cannot be mistaken for English: `remember` is also a word, so it is not looked for in running text.
LOOKED_FOR = {name for domain in DOMAINS for name in _tool_names(domain) if "_" in name} | {"calculator"}


def named_but_missing(text: str, have: set[str]) -> set[str]:
    return {name for name in LOOKED_FOR - have if re.search(rf"\b{name}\b", text)}


def _model_facing(domain: str) -> dict[str, str]:
    """Everything the model of `domain` reads before it has done anything, by where it came from."""
    manifest, plugin = DOMAINS[domain]
    texts = {"system prompt": compose_system_prompt(manifest.system_prompt)}
    texts |= {f"tool {tool.name}": json.dumps(convert_to_openai_tool(tool), ensure_ascii=False) for tool in plugin.tools()}
    return texts


@pytest.mark.parametrize("domain", sorted(DOMAINS))
def test_a_domains_prompt_and_tool_descriptions_name_only_its_own_tools(domain):
    have = _tool_names(domain)

    missing = {where: found for where, text in _model_facing(domain).items() if (found := named_but_missing(text, have))}

    assert missing == {}, "the model is told about a tool it does not have: reword, or put the sentence where only the owning domain reads it"


@pytest.mark.parametrize("name", sorted(get_subagents()))
def test_a_subagents_prompt_names_only_tools_it_is_given(name):
    record = get_subagents()[name]

    assert named_but_missing(record.system_prompt, set(record.tools or ())) == set()


def test_nothing_the_model_reads_is_a_note_to_a_developer():
    texts = {f"{domain}: {where}": text for domain in DOMAINS for where, text in _model_facing(domain).items()}
    texts |= {f"skill {name}": record.body for name, record in load_skills().items()}
    texts |= {f"subagent {name}": record.system_prompt for name, record in get_subagents().items()}

    found = {where: hits for where, text in texts.items() if (hits := sorted(set(DEVELOPER_NOTE.findall(text))))}

    assert found == {}, "move it to a comment above the code: a tool's docstring is its description"


def test_the_checks_can_fail():
    assert named_but_missing("only when calculator's plain arithmetic isn't enough, then query_employees", {"search_docs"}) == {"calculator", "query_employees"}
    assert named_but_missing("call search_docs, and remember to cite", {"search_docs"}) == set()
    for note in ("(GRAPH_PATTERNS.md pattern 27)", "the SYSTEM_PROMPT instructs", "declared in TOOL_CAPABILITIES", "see app/agent/graph/core.py"):
        assert DEVELOPER_NOTE.search(note), note
