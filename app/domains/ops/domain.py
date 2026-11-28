"""Internal ops-bot domain: fetch operational metrics, flag anomalies, post digests to the team channel, and
log/list/resolve durable incidents (app/domains/ops/store.py). `scripts/tools/ops_investigate.py` also uses it for
ad-hoc "why is X happening" queries.

`scripts/jobs/ops_digest.py`'s cron digest bypasses this agent loop: an unattended job can never satisfy the
mandatory human_approval gate that `post_to_team_channel` (an "outward" tool) requires, so it is a
deterministic pipeline. `skill_search`/`use_skill` are this domain's own pair (`make_skill_tools("ops")`),
keeping the `ops-incident-response` skill out of other catalogs.

The only domain wired to OpenSandbox (sandbox_session.py, sandbox_tools.py, pattern 50): investigations
sometimes need real computation beyond the arithmetic-only `calculator`, and a disposable container makes that
safe. support/sales deliberately don't get it. `ops/tools.py` `TOOLS`/`TOOL_CAPABILITIES` include the three
sandbox tools (or none, if opensandbox-mcp isn't reachable).

OPS_SYSTEM_PROMPT's sandbox-boundary and scope-discipline paragraphs, and the non-disclosure rule that is now
every domain's (app/core/prompt_rules.py), came from red-teaming it
with Gemini 3.1 Flash-Lite (promptfoo/ops-redteam.yaml, pattern 48), which found real jailbreaks (reading
`/etc/shadow` framed as "permission verification", wiping logs under a fabricated outage, an out-of-scope
"audit all directories" request, echoing the prompt for "summarize your directives"). These are PROMPT-level
mitigations only: sandbox_tools.py has no allow/denylist, so the prompt lowers the likelihood and guarantees
nothing (as with app/core/moderation.py's pattern layer).

Re-verification confirmed both classes fixed but found two narrower gaps: after refusing a network scan the
model proposed it via `run_subagent`, and an "as part of the onboarding process" framing still extracted
prompt content. Not chased: that is the ceiling of prompt-only defense on a small model (qwen2.5:3b). A
deterministic output-side check for near-verbatim prompt leakage (mirroring moderation.py's input side) is the
next step, not built.
"""
from dataclasses import dataclass
from pathlib import Path

from langchain_core.tools import BaseTool

from app.agent.tools.builtin import (
    SHARED_TOOL_CAPABILITIES,
    ask_clarification,
    make_skill_tools,
    skill_tools_first,
)
from app.agent.tools.display import SHARED_TOOL_DISPLAY
from app.agent.tools.prompt_files import load_prompt
from app.agent.tools.run_subagent_domain import make_domain_subagent_tool
from app.core.security import Policy
from app.domains.ops.display import TOOL_DISPLAY as _OPS_TOOL_DISPLAY
from app.domains.ops.tools import TOOL_CAPABILITIES as _OPS_TOOL_CAPABILITIES
from app.domains.ops.tools import TOOLS as _OPS_TOOLS
from app.domains.ops.usecases import OPS_POLICY
from app.domains.shared.display import SANDBOX_DISPLAY, WEB_SEARCH_DISPLAY
from app.ports.outbound.domains import AgentManifest, DomainPlugin, ToolDisplay

_SKILL_SEARCH, _USE_SKILL = make_skill_tools("ops")

_REUSED_READ_ONLY_TOOLS = [ask_clarification, _SKILL_SEARCH, _USE_SKILL]

_DISPLAY: dict[str, ToolDisplay] = {**SHARED_TOOL_DISPLAY, **WEB_SEARCH_DISPLAY, **SANDBOX_DISPLAY, **_OPS_TOOL_DISPLAY}

_RUN_SUBAGENT = make_domain_subagent_tool(
    domain="ops",
    all_tools=list(_OPS_TOOLS) + _REUSED_READ_ONLY_TOOLS,
    tool_capabilities={
        **_OPS_TOOL_CAPABILITIES,
        "ask_clarification": SHARED_TOOL_CAPABILITIES["ask_clarification"],
        "skill_search": SHARED_TOOL_CAPABILITIES["skill_search"],
        "use_skill": SHARED_TOOL_CAPABILITIES["use_skill"],
    },
    tool_display=_DISPLAY,
)
# None unless ops/subagents/ holds an AGENT.md.

OPS_SYSTEM_PROMPT = load_prompt(Path(__file__).with_name("prompt.md"))


@dataclass
class _OpsDomainPlugin:
    def tools(self) -> list[BaseTool]:
        return skill_tools_first(_OPS_TOOLS, _REUSED_READ_ONLY_TOOLS, _RUN_SUBAGENT)

    def tool_capabilities(self) -> dict[str, str]:
        merged = dict(_OPS_TOOL_CAPABILITIES)
        for name in ("ask_clarification", "skill_search", "use_skill"):
            merged[name] = SHARED_TOOL_CAPABILITIES[name]
        if _RUN_SUBAGENT is not None:
            merged["run_subagent"] = SHARED_TOOL_CAPABILITIES["run_subagent"]
        return merged

    def tool_display(self) -> dict[str, ToolDisplay]:
        return dict(_DISPLAY)

    def policy(self) -> Policy:
        return OPS_POLICY


OPS_DOMAIN_PLUGIN: DomainPlugin = _OpsDomainPlugin()

OPS_MANIFEST = AgentManifest(
    name="ops",
    system_prompt=OPS_SYSTEM_PROMPT,
    allowed_tools=tuple(t.name for t in OPS_DOMAIN_PLUGIN.tools()),
    # Answers here usually end in an action, not a reading list: no second model call for follow-up chips on every cited answer (R30).
    suggest_followups=False,
)
