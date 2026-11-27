"""The support-copilot domain: a Tier-1 customer-facing agent, deployed
behind a chat gateway (app/adapters/inbound/channels/telegram/bot.py's AGENT_DOMAIN), with
SANDBOXED tool access — just the knowledge base plus the ticket system.

`tools()` reuses `search_docs`/`ask_clarification` from app/agent/tools/builtin.py
as-is rather than reimplementing retrieval. `skill_search`/`use_skill` are
this domain's own pair (`make_skill_tools("support")`), so a
`SKILL.md` in support/skills/ stays scoped to this domain.
`SUPPORT_MANIFEST.allowed_tools` is exactly those four plus this domain's
five ticket tools — deliberately excluding
calculator/add_note/remember/query_employees: a Tier-1 support bot has no
legitimate reason for arbitrary arithmetic, writing to the shared
knowledge base, remembering cross-session facts, or looking up employees.
That omission (not a Policy check) is literally what "sandboxed" means
here — `build_graph()`'s `ToolNode` only ever knows the tools this list
names (pattern 23).
"""
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool

from app.agent.tools.builtin import (
    SHARED_TOOL_CAPABILITIES,
    ask_clarification,
    make_skill_tools,
    search_docs,
    skill_tools_first,
)
from app.agent.tools.prompt_files import load_prompt
from app.agent.tools.run_subagent_domain import make_domain_subagent_tool
from app.core.security import Policy
from app.domains.support.tools import SUPPORT_POLICY
from app.domains.support.tools import TOOL_CAPABILITIES as _SUPPORT_TOOL_CAPABILITIES
from app.domains.support.tools import TOOLS as _SUPPORT_TOOLS
from app.ports.outbound.domains import AgentManifest, DomainPlugin

_SKILL_SEARCH, _USE_SKILL = make_skill_tools("support")

_REUSED_READ_ONLY_TOOLS = [search_docs, _SKILL_SEARCH, _USE_SKILL, ask_clarification]

_RUN_SUBAGENT = make_domain_subagent_tool(
    domain="support",
    all_tools=list(_SUPPORT_TOOLS) + _REUSED_READ_ONLY_TOOLS,
    tool_capabilities={
        **_SUPPORT_TOOL_CAPABILITIES,
        "search_docs": SHARED_TOOL_CAPABILITIES["search_docs"],
        "skill_search": SHARED_TOOL_CAPABILITIES["skill_search"],
        "use_skill": SHARED_TOOL_CAPABILITIES["use_skill"],
        "ask_clarification": SHARED_TOOL_CAPABILITIES["ask_clarification"],
    },
)
# None unless support/subagents/ holds an AGENT.md —
# see make_domain_subagent_tool's own docstring for why an empty menu means
# "don't expose the tool at all" rather than a real, empty-choice tool.

SUPPORT_SYSTEM_PROMPT = load_prompt(Path(__file__).with_name("prompt.md"))


@dataclass
class _SupportDomainPlugin:
    def tools(self) -> list[BaseTool]:
        return skill_tools_first(_SUPPORT_TOOLS, _REUSED_READ_ONLY_TOOLS, _RUN_SUBAGENT)

    def tool_capabilities(self) -> dict[str, str]:
        merged = dict(_SUPPORT_TOOL_CAPABILITIES)
        for name in ("search_docs", "skill_search", "use_skill", "ask_clarification"):
            merged[name] = SHARED_TOOL_CAPABILITIES[name]
        if _RUN_SUBAGENT is not None:
            merged["run_subagent"] = SHARED_TOOL_CAPABILITIES["run_subagent"]
        return merged

    def policy(self) -> Policy:
        return SUPPORT_POLICY


SUPPORT_DOMAIN_PLUGIN: DomainPlugin = _SupportDomainPlugin()

SUPPORT_MANIFEST = AgentManifest(
    name="support",
    system_prompt=SUPPORT_SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in SUPPORT_DOMAIN_PLUGIN.tools()),
    # Answers here usually end in an action, not a reading list: no second model call for follow-up chips on every cited answer (R30).
    suggest_followups=False,
)
