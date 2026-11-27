"""The Ecorp domain: the default agent this app ships. Same shape as the other three
(`store.py` + `tools.py` + `domain.py`), and it runs on the same unmodified graph.

`tools()` is the shared retrieval/skill tools from `app/agent/tools/builtin.py` plus this
domain's `add_note`/`remember`/`query_employees`, in the order live testing found
the small local model needs (`skill_tools_first`: skill_search/use_skill/run_subagent
lead, pattern 50). `run_subagent` is built by the same `make_domain_subagent_tool`
every other domain uses, and is absent when no bundled AGENT.md declares this domain.
"""
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool

from app.agent.tools.builtin import (
    SHARED_TOOL_CAPABILITIES,
    ask_clarification,
    calculator,
    make_skill_tools,
    search_docs,
    skill_tools_first,
)
from app.agent.tools.prompt_files import load_prompt
from app.agent.tools.run_subagent_domain import make_domain_subagent_tool
from app.core.security import DEFAULT_POLICY, Policy
from app.domains.ecorp.tools import TOOL_CAPABILITIES as _ECORP_TOOL_CAPABILITIES
from app.domains.ecorp.tools import TOOLS as _ECORP_TOOLS
from app.ports.outbound.domains import AgentManifest, DomainPlugin

# Gotchas of prompt.md, found via live testing with qwen2.5:3b (not just
# inspection) — don't reintroduce these:
# 1. search_docs vs query_employees: "support hours" collided with the
#    Department.support enum, so business-hours questions mis-routed to
#    query_employees. Fixed by naming the exact failure mode in the prompt
#    (both tools' schemas were already correct).
# 2. Keep the calculator instruction's wording away from "hours" — an
#    earlier fix phrased it right next to "hours" and caused "Ecorp support
#    hours" to mis-route to the calculator instead (same word-collision
#    class, just relocated).
# 3. Small-model instruction-following is proximity-sensitive: a rule
#    stated once, early, in a general style paragraph isn't reliably
#    followed right after a tool result. check_output's
#    `defers_instead_of_acting` caught the model appending a
#    permission-seeking closer (already forbidden earlier in the prompt)
#    in roughly half of real runs; repeating a SHORTER version of the same
#    rule right after the citation-marker instructions (closest to the
#    failure point) dropped that to 1/12, live-verified.
# The text is prompt.md. It is static, deliberately (pattern 19): never fill it with request-specific data (ctx, timestamps, trace ids), which
# breaks prompt-cache stability silently. Guarded by tests/agent/graph/test_prompt_cache_stability.py.
ECORP_SYSTEM_PROMPT = load_prompt(Path(__file__).with_name("prompt.md"))

_SKILL_SEARCH, _USE_SKILL = make_skill_tools("ecorp")

# Only Ecorp has a staff directory, and "support hours" used to go to it (gotcha 1 above): the shared tool's description cannot name a tool
# other domains lack, so Ecorp adds the boundary to its own copy.
_SEARCH_DOCS = search_docs.model_copy(
    update={
        "description": f"{search_docs.description} "
        'That includes one whose wording happens to overlap with a department name (e.g. "support hours" is a knowledge-base fact '
        'about business hours, not a staff lookup, even though "Support" is also a department query_employees can filter by). '
        "Only use query_employees instead when the question is actually asking WHO works somewhere — a specific person, a roster, or headcount."
    }
)

_REUSED_TOOLS = [_SKILL_SEARCH, _USE_SKILL, ask_clarification]
_ACTION_TOOLS = [_SEARCH_DOCS, calculator, *_ECORP_TOOLS]

_CAPABILITIES: dict[str, str] = {**SHARED_TOOL_CAPABILITIES, **_ECORP_TOOL_CAPABILITIES}

_RUN_SUBAGENT = make_domain_subagent_tool(
    domain="ecorp",
    all_tools=[*_ACTION_TOOLS, *_REUSED_TOOLS],
    tool_capabilities=_CAPABILITIES,
)
# None unless Ecorp's subagents/ folder holds an AGENT.md — see
# make_domain_subagent_tool for why an empty menu means "don't expose the tool at all".


@dataclass
class _EcorpDomainPlugin:
    def tools(self) -> list[BaseTool]:
        return skill_tools_first(_ACTION_TOOLS, _REUSED_TOOLS, _RUN_SUBAGENT)

    def tool_capabilities(self) -> dict[str, str]:
        capabilities = dict(_CAPABILITIES)
        if _RUN_SUBAGENT is None:
            capabilities.pop("run_subagent", None)
        return capabilities

    def policy(self) -> Policy:
        return DEFAULT_POLICY


ECORP_DOMAIN_PLUGIN: DomainPlugin = _EcorpDomainPlugin()

ECORP_MANIFEST = AgentManifest(
    name="ecorp",
    system_prompt=ECORP_SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in ECORP_DOMAIN_PLUGIN.tools()),
)
