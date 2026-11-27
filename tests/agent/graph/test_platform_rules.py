"""Every agent, domain or specialist, is sent the platform's rules (app/core/prompt_rules.py, GRAPH_PATTERNS.md pattern 114).

The rule that gives `frame_untrusted`'s tags their meaning was written into one domain's prompt only, so support, ops and sales (the three
that read web pages) were never told what the tags were. Reading each prompt would not have caught a fifth domain doing the same, so these
build the real graph for every registered domain and every bundled subagent and look at what the model receives.
"""
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.agent.graph.build import GraphDeps, build_graph
from app.agent.graph.output_guardrails import reveals_system_prompt
from app.agent.tools.run_subagent import build_subagent_registry, run_subagent_impl
from app.agent.tools.subagent_catalog import get_subagents
from app.core.prompt_rules import (
    NON_DISCLOSURE_RULE,
    PLATFORM_RULES,
    UNTRUSTED_DATA_RULE,
    compose_system_prompt,
)
from app.core.untrusted import CLOSE_TAG, OPEN_TAG
from app.domains.registry import DOMAINS
from tests.agent.fakes import cfg, scripted_llm

ANSWER = AIMessage(content="A sufficiently long answer here.")
SUBAGENTS = sorted((record.domains[0], name) for name, record in get_subagents().items())


async def _no_context(query, ctx):
    return "", []


@pytest.mark.parametrize("domain", sorted(DOMAINS))
async def test_every_domain_is_sent_the_platform_rules_after_its_own_prompt(domain):
    manifest, plugin = DOMAINS[domain]
    seen: list = []
    graph = build_graph(manifest, plugin, GraphDeps(llm=scripted_llm(lambda _m: ANSWER, seen), search_docs=_no_context))

    await graph.ainvoke({"messages": [HumanMessage(content="a question here")]}, config=cfg())

    system = seen[0][0]
    assert isinstance(system, SystemMessage)
    assert system.content == f"{manifest.system_prompt}\n\n{PLATFORM_RULES}"


@pytest.mark.parametrize(("domain", "name"), SUBAGENTS, ids=[f"{d}:{n}" for d, n in SUBAGENTS])
async def test_every_subagent_is_sent_the_platform_rules(domain, name):
    plugin = DOMAINS[domain][1]
    tools = {tool.name: tool for tool in plugin.tools()}
    seen: list = []

    await run_subagent_impl(
        name,
        "look something up",
        cfg(),
        domain=domain,
        registry=build_subagent_registry(frozenset(tools), plugin.tool_capabilities(), domain=domain),
        tools_by_name=tools,
        llm=scripted_llm(lambda _m: ANSWER, seen),
    )

    system = seen[0][0].content
    assert system.startswith(get_subagents()[name].system_prompt)
    assert system.endswith(PLATFORM_RULES)


def test_the_rule_names_the_tags_the_frame_really_uses():
    assert OPEN_TAG in UNTRUSTED_DATA_RULE
    assert CLOSE_TAG == OPEN_TAG.replace("<", "</")


@pytest.mark.parametrize("domain", sorted(DOMAINS))
def test_no_domain_keeps_its_own_copy_of_a_platform_rule(domain):
    """One place to edit: a second copy is how the first one came to differ between domains."""
    prompt = DOMAINS[domain][0].system_prompt.lower()

    assert "retrieved_document" not in prompt
    assert "never reveal, quote" not in " ".join(prompt.split())


@pytest.mark.parametrize("domain", sorted(DOMAINS))
def test_the_leak_check_still_keys_on_the_domains_own_opening_and_now_catches_the_rules(domain):
    composed = compose_system_prompt(DOMAINS[domain][0].system_prompt)

    assert composed.startswith("You are")
    assert reveals_system_prompt(f"Sure: {composed[:40]}", composed)
    assert reveals_system_prompt(f"My rules say: {NON_DISCLOSURE_RULE}", composed)
    assert not reveals_system_prompt("I can't share my internal configuration, but I'm glad to help with the task.", composed)
