"""The graph builder. `build_graph` compiles every graph this app runs; a `GraphProfile` says which optional
stages that graph includes:

- `FULL_PROFILE`: the main conversation (semantic cache, history compaction, follow-up suggestions, citation
  enforcement).
- `SUBAGENT_PROFILE`: a nested `run_subagent` run (pattern 46), with none of those. `build_subagent_graph` is
  `build_graph` with that profile.

Topology: `validate_input -> moderate_input -> [check_semantic_cache -> (miss) [compact_history ->]]
[route_model || retrieve_context] -> agent <-> tools` (the router and retrieval are one parallel step, `agent` waits for both, and
a domain whose manifest says `prefetch="never"` has no `retrieve_context`), then `check_output`, which retries, gives up, or ends
(through `[suggest_followups] || [write_semantic_cache]`, in parallel). Moderation precedes anything that
spends: a refused or cache-hit turn pays for no compaction (pattern 25) and asks no model router (pattern 68).
GRAPH_PATTERNS.md "Graph Flow" draws it.

`GraphDeps` bundles the swappable external clients, built once here and threaded into the node factories;
unset fields fall back to the real clients, and tests set individual fields to inject fakes instead of
monkeypatching module globals.

`manifest` and `domain` are required: one function serves any domain (prompt, tools, capabilities, policy)
with no branching and no default. Ecorp is one entry in `app/domains/registry.py`.

This is the composition root, so the one module that imports every node; nothing a node imports imports it
back, which is why no graph module needs a deferred import.

Names (GRAPH_PATTERNS.md "Naming"): `graph_node_<node>` is the function of the node registered as `"<node>"` and `make_graph_node_<node>` the
factory that builds it; `graph_edge_route_after_<node>` is the router of that node's conditional edge. The registered string is the node's
identity (traces, metrics, checkpoints); `EDGE_CHANNEL_NAMES` is the edge's.
"""
import functools
from collections.abc import Awaitable, Callable, Hashable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt

from app.agent.graph.agent_node import make_graph_node_agent
from app.agent.graph.cache import (
    graph_edge_route_after_check_semantic_cache,
    make_graph_node_check_semantic_cache,
    make_graph_node_write_semantic_cache,
)
from app.agent.graph.compaction import make_graph_node_compact_history
from app.agent.graph.core import (
    AGENT_RETRY_POLICY,
    graph_edge_route_after_compact_history,
    graph_edge_route_after_moderate_input,
    graph_edge_route_after_validate_input,
    graph_node_context_window_exceeded,
    graph_node_moderate_input,
    graph_node_reject_context,
    graph_node_reject_input,
    graph_node_reject_moderation,
    graph_node_validate_input,
)
from app.agent.graph.followups import make_graph_node_suggest_followups
from app.agent.graph.hitl import (
    graph_edge_route_after_human_approval,
    make_graph_node_human_approval,
)
from app.agent.graph.links import cannot_open_links
from app.agent.graph.model_route import make_graph_node_route_model
from app.agent.graph.output_checks import parse_checks_off_on_routed_tiers
from app.agent.graph.retrieval import make_graph_node_retrieve_context
from app.agent.graph.retry import (
    graph_node_retry_output,
    make_graph_node_no_answer,
    make_graph_node_retry_exhausted,
)
from app.agent.graph.routing import (
    graph_edge_route_after_agent,
    graph_edge_route_after_check_output,
    graph_node_check_output,
)
from app.agent.graph.skill_guards import SKILL_REQUIRED_TOOL_MARKERS
from app.agent.graph.state import (
    CONTEXT_GAP_REASON,
    HISTORY_TOKEN_CEILING,
    HISTORY_TOKEN_FLOOR,
    MAX_ITERATIONS,
    MAX_TOKENS_PER_TURN,
    State,
)
from app.agent.graph.stream_events import (
    announcing,
    judged_the_answer,
    rejected_the_answer,
    replaced_the_answer,
    trimmed_the_history,
)
from app.agent.graph.tool_guards import graph_node_tool_batch_guard
from app.agent.graph.utils import (
    NodeFn,
    RouterFn,
    friendly_tool_error,
    instrumented,
    make_llm,
    named_edge,
)
from app.agent.tools import builtin
from app.core.config import (
    CHAT_MODEL,
    CONTEXT_GAP_REASK_ENABLED,
    FOLLOWUPS_ENABLED,
    MAX_COST_USD_PER_TURN,
    OUTPUT_CHECKS_OFF_ON_ROUTED_TIERS,
    SEMANTIC_CACHE_ENABLED,
)
from app.core.security import SecurityCtx
from app.ports.outbound.retrieval import answer_cache

if TYPE_CHECKING:
    from app.ports.outbound.domains import AgentManifest, DomainPlugin


async def default_cache_get(ctx: SecurityCtx | None, query: str, *, domain: str) -> tuple[str, list[dict[str, Any]]] | None:
    return await answer_cache.get().get(ctx, query, domain=domain)


async def default_cache_set(ctx: SecurityCtx | None, query: str, answer: str, citations: list[dict[str, Any]], *, domain: str) -> None:
    await answer_cache.get().set(ctx, query, answer, citations, domain=domain)


async def default_search(query: str, ctx: SecurityCtx | None) -> tuple[str, list[dict[str, Any]]]:
    """Thin wrapper over `builtin.gather_context` matching the
    `Callable[[str, SecurityCtx | None], Awaitable[tuple[str, list[dict]]]]`
    shape `make_graph_node_retrieve_context` expects, so tests can pass a plain
    fake function to the factory.

    `ctx` flows straight through — the same value search_docs/remember read
    from config when the *model* calls them as tools, so pre-fetched and
    on-demand retrieval are policy-enforced identically. Hybrid search
    (dense+sparse RRF, cross-encoder reranked) and cross-session memory
    recall both live inside `gather_context` (see app/agent/tools/builtin.py, pattern 20).

    async: gather_context awaits real I/O (the reranker is an HTTP call to
    ml-service, not local ONNX compute).
    """
    return await builtin.gather_context(ctx, query)


@dataclass
class GraphDeps:
    """Bundle of the graph's swappable external clients, built once at the
    composition root (build_graph) and threaded into the node factories
    that need them — see make_graph_node_agent, make_graph_node_retrieve_context.
    Unset fields fall back to the real clients inside build_graph(); tests
    set individual fields to inject fakes instead of monkeypatching module
    globals (see tests/agent/graph/test_agent_node.py, tests/agent/graph/test_nodes.py).
    """

    llm: Any = None
    # The LiteLLM alias `llm` is bound to, used only to price its calls
    # (app/agent/cost/pricing.py). None = the global chat alias; a delegated run whose
    # specialist declares its own model sets it so its calls are priced by that
    # model, not by the parent's.
    model_alias: str | None = None
    # The usage-event `kind` this client's calls are recorded under (app/agent/cost/usage_events.py).
    # "chat" for the main loop; a delegated run sets "subagent" so its spend is separable.
    meter_kind: str = "chat"
    search_docs: (
        Callable[[str, "SecurityCtx | None"], Awaitable[tuple[str, list[dict[str, Any]]]]] | None
    ) = None
    cache_get: (
        Callable[["SecurityCtx | None", str], Awaitable[tuple[str, list[dict[str, Any]]] | None]] | None
    ) = None
    cache_set: (
        Callable[["SecurityCtx | None", str, str, list[dict[str, Any]]], Awaitable[None]] | None
    ) = None
    # Builds the chat client from the domain's tools when `llm` is unset (default: `make_llm`, the
    # configured endpoint). A test pointing the graph at a local Ollama passes
    # `functools.partial(make_llm, model=..., base_url=...)` here instead of patching a module.
    llm_factory: Callable[[list[Any]], Any] | None = None
    # The function that pauses the graph at `human_approval` (default: langgraph's `interrupt`). A
    # test that wants an approval decision without driving a real pause passes a stand-in.
    interrupt: Callable[[Any], Any] | None = None


@dataclass(frozen=True)
class GraphProfile:
    """Which optional stages a graph includes. A stage that is absent is not a no-op node: it is
    not in the graph, and its neighbours are wired straight to the next one.

    `enforce_citations=False` drops the two places a graph pushes `[n]` markers on the model: the
    tail "every sentence must end with its marker" reminder in `agent`, and `check_output`'s
    uncited finding, marker insertion and the dropping of an unsupported marker. A nested run's sources are not the
    parent's, so its prompt says to use none.
    """

    include_cache: bool = True
    include_followups: bool = True
    include_compaction: bool = True
    enforce_citations: bool = True
    # `route_model` (specs/014): may send a turn to the stronger model. On for the conversation; off for a nested run, which has its own
    # model and budgets and where the user's message is not the delegated task.
    include_router: bool = True


FULL_PROFILE = GraphProfile()

# A nested, one-shot subagent run (pattern 46). The omitted stages are pure overhead (or
# worse) here:
# - check_semantic_cache / write_semantic_cache: a subagent run shared the top-level
#   conversation's own cache (keyed by tenant+principal), risking a cross-contaminated hit in
#   either direction.
# - route_model: a nested run uses the specialist's own model and the parent's router never saw the delegated task.
# - suggest_followups: a full extra LLM call whose result the caller simply discarded.
# - compact_history / context_window_exceeded: provably inert, not just unlikely. Compaction only
#   fires past HISTORY_TOKEN_CEILING (24000), but a subagent run is hard-capped at
#   MAX_SUBAGENT_TOKENS_PER_RUN (4000).
# Deliberately KEPT despite looking like overhead: moderate_input (cheap, and the delegated `task`
# came from the PARENT model, not a person), retrieve_context (a lookup-focused subagent is usually
# well served by the same pre-fetch), and human_approval (currently unreachable here since only
# read_only tools are allowed, but it stays as defense in depth: no flag turns the mandatory gate off).
SUBAGENT_PROFILE = GraphProfile(
    include_cache=False,
    include_followups=False,
    include_compaction=False,
    enforce_citations=False,
    include_router=False,
)


# The name a checkpoint records for each node's conditional edge (source node -> name), which is what a turn a worker left mid-flight is
# resumed through (GRAPH_PATTERNS.md 16). These are the names the graph had before routers became `graph_edge_route_after_<node>`, held
# unchanged on purpose: `named_edge` registers each router under its entry, so renaming a router function cannot strand a turn. Three
# edges are `route` because `then()` builds their router, and `agent`'s is `condition` because its router is a `functools.partial`.
# A new conditional edge gets `graph_edge_route_after_<node>` here. `tests/agent/graph/test_edge_channel_names.py` holds the table
# equal to the compiled graph; changing an entry is a topology change, so drain the workers first.
EDGE_CHANNEL_NAMES: Mapping[str, str] = {
    "validate_input": "route_after_validation",
    "moderate_input": "route",
    "check_semantic_cache": "route",
    "compact_history": "route",
    "agent": "condition",
    "human_approval": "route_after_approval",
    "check_output": "route_after_check_bound",
}


def build_graph(
    manifest: "AgentManifest",
    domain: "DomainPlugin",
    deps: GraphDeps | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    max_iterations: int | None = None,
    max_tokens_per_turn: int | None = None,
    max_cost_usd_per_turn: float | None = None,
    *,
    emit_no_answer_message: bool = True,
    history_token_ceiling: int | None = None,
    history_token_floor: int | None = None,
    profile: GraphProfile = FULL_PROFILE,
) -> CompiledStateGraph:
    """Compile the graph.

    `deps` bundles the swappable clients (LLM, search); tests inject fakes through a `GraphDeps` to run full
    scenarios (reject path, tool loop, HITL, iteration cap, retry) without a live model or Qdrant
    (tests/agent/graph/test_graph_integration.py).

    `checkpointer` defaults to an in-memory MemorySaver: fine for tests, never for a real HITL pause (a paused
    `human_approval` gate parks the run indefinitely, and MemorySaver doesn't survive a restart).
    `init_graph_async()` in app/agent/runtime/core.py passes a durable AsyncPostgresSaver for the CLI/API
    singleton.

    `max_iterations`/`max_tokens_per_turn`/`max_cost_usd_per_turn` default to `None` (the
    MAX_ITERATIONS/MAX_TOKENS_PER_TURN/MAX_COST_USD_PER_TURN constants); only `run_subagent` sets them, to the
    MAX_SUBAGENT_* ceilings (pattern 46).

    `emit_no_answer_message` (default True): whether `no_answer` AND `retry_exhausted` fill an empty final
    AIMessage with a user-facing fallback. `run_subagent` sets False because it substitutes its own
    differently worded message and outcome tag, and the real fallback would be read as the subagent's genuine
    answer.

    `history_token_ceiling`/`history_token_floor` default to `None` (HISTORY_TOKEN_CEILING/FLOOR); no
    production caller overrides them, they let tests exercise compact_history's hysteresis with small budgets.
    """
    deps = deps or GraphDeps()

    domain_tools = domain.tools()
    if manifest.allowed_tools:
        allowed = set(manifest.allowed_tools)
        domain_tools = [t for t in domain_tools if t.name in allowed]
    tool_capabilities = domain.tool_capabilities()
    valid_tool_names = frozenset(t.name for t in domain_tools)
    # A skill may name a tool as required; only ones THIS domain has can ever be demanded.
    skill_required_tools = tuple(m for m in SKILL_REQUIRED_TOOL_MARKERS if m in valid_tool_names)

    llm_client = deps.llm or (deps.llm_factory or make_llm)(domain_tools)
    model_alias = deps.model_alias or CHAT_MODEL

    # Plain module-level functions bound to THIS domain with functools.partial, not factories:
    # they stay directly callable from tests with just `state` (and the domain's values), and
    # each is checked against the capability mapping / tool set / prompt the graph was ACTUALLY
    # built with (pattern 23).
    graph_edge_route_after_agent_bound = functools.partial(
        graph_edge_route_after_agent,
        tool_capabilities=tool_capabilities,
        valid_tool_names=valid_tool_names,
        max_iterations=max_iterations if max_iterations is not None else MAX_ITERATIONS,
        max_tokens=max_tokens_per_turn if max_tokens_per_turn is not None else MAX_TOKENS_PER_TURN,
        max_cost_usd=max_cost_usd_per_turn if max_cost_usd_per_turn is not None else MAX_COST_USD_PER_TURN,
    )
    graph_node_check_output_bound = functools.partial(
        graph_node_check_output,
        system_prompt=manifest.system_prompt,
        enforce_citations=profile.enforce_citations,
        skill_required_tools=skill_required_tools,
        tool_names=valid_tool_names,
        # Validated here, at build, so a typo or a protected check stops the process at start-up instead of leaving a check running that the operator thinks is off.
        skip_on_routed_tiers=parse_checks_off_on_routed_tiers(OUTPUT_CHECKS_OFF_ON_ROUTED_TIERS),
        checks_off=frozenset() if CONTEXT_GAP_REASK_ENABLED else frozenset({CONTEXT_GAP_REASON}),
    )
    graph_node_tool_batch_guard_bound = functools.partial(graph_node_tool_batch_guard, valid_tool_names=valid_tool_names)

    graph_node_agent = make_graph_node_agent(
        llm_client,
        model_alias=model_alias,
        kind=deps.meter_kind,
        citation_reminder=profile.enforce_citations,
        skill_required_tools=skill_required_tools,
        cannot_open_links=cannot_open_links(valid_tool_names),
        system_prompt=manifest.system_prompt,
        hold_context_gap=CONTEXT_GAP_REASK_ENABLED,
    )
    graph_node_retrieve_context = make_graph_node_retrieve_context(deps.search_docs or default_search)

    # Error recovery: a failing tool (e.g. Qdrant unreachable) doesn't crash the run —
    # handle_tool_errors turns the exception into a ToolMessage so `agent` can react on the next
    # turn instead of the graph blowing up. Parallel: ToolNode already runs multiple tool_calls
    # from one AIMessage concurrently — no extra wiring needed. Wrapped in a function so it gets
    # the same lifecycle logging as every other node (pattern 14); the ToolNode does the work and
    # its result (messages, or Commands from a tool like run_subagent) is returned unchanged.
    tool_node = ToolNode(domain_tools, handle_tool_errors=friendly_tool_error)

    async def graph_node_tools(state: State, config: RunnableConfig) -> Any:
        return await tool_node.ainvoke(state, config)

    builder = StateGraph(State)

    # Every node below is wrapped in instrumented(name) at registration time, not by editing the
    # node functions themselves (pattern 14). Plain module-level functions stay undecorated so
    # they're directly callable from tests.
    def add(name: str, fn: NodeFn, **kwargs: Any) -> None:
        builder.add_node(name, instrumented(name)(fn), **kwargs)

    add("validate_input", graph_node_validate_input)
    add("reject_input", graph_node_reject_input)
    add("reject_context", graph_node_reject_context)
    add("moderate_input", graph_node_moderate_input)
    add("reject_moderation", graph_node_reject_moderation)
    prefetches = manifest.prefetch == "always"  # "never": the node is not in the graph at all (R29)
    if prefetches:
        add("retrieve_context", graph_node_retrieve_context)
    # Reliability policy: retry a transient LLM-endpoint failure a few times before giving up —
    # see AGENT_RETRY_POLICY. Nothing else here gets one: `tools` recovers via handle_tool_errors
    # above, and every other node is a pure function of state where a retry would just repeat the
    # same bug (pattern 7).
    add("agent", graph_node_agent, retry=AGENT_RETRY_POLICY)
    add("tools", graph_node_tools)
    add("human_approval", make_graph_node_human_approval(deps.interrupt or interrupt))
    add("tool_batch_guard", graph_node_tool_batch_guard_bound)
    # These four announce what the client should see (`stream_events.py`) instead of the runtime recognising them by name.
    add("check_output", announcing(graph_node_check_output_bound, judged_the_answer))
    add("retry_output", announcing(graph_node_retry_output, rejected_the_answer))
    add("retry_exhausted", announcing(make_graph_node_retry_exhausted(emit_message=emit_no_answer_message), replaced_the_answer))
    add(
        "no_answer",
        make_graph_node_no_answer(
            emit_message=emit_no_answer_message,
            system_prompt=manifest.system_prompt,
            enforce_citations=profile.enforce_citations,
            skill_required_tools=skill_required_tools,
            tool_names=valid_tool_names,
        ),
    )

    if profile.include_cache:
        # Read as bare module globals, resolved when build_graph runs: tests/conftest.py's autouse
        # fixtures replace default_cache_get/default_cache_set on THIS module.
        # The default cache is bound to THIS graph's domain (`manifest.name`) here, in the one place that knows it: `app/agent/runtime/core.py` knows it too,
        # but imports this module. A test's injected `cache_get`/`cache_set` is used as given.
        # SEMANTIC_CACHE_ENABLED false makes both nodes return at once; they stay in the graph so the topology does not depend on a setting.
        add(
            "check_semantic_cache",
            make_graph_node_check_semantic_cache(deps.cache_get or functools.partial(default_cache_get, domain=manifest.name), enabled=SEMANTIC_CACHE_ENABLED),
        )
        add(
            "write_semantic_cache",
            make_graph_node_write_semantic_cache(
                deps.cache_set or functools.partial(default_cache_set, domain=manifest.name), tool_capabilities, enabled=SEMANTIC_CACHE_ENABLED
            ),
        )
    if profile.include_compaction:
        # Reuses the SAME llm client as `agent` (a summarization call doesn't need tools bound; a
        # tool-bound client asked a plain prompt just answers it).
        add(
            "compact_history",
            announcing(
                make_graph_node_compact_history(
                    llm_client,
                    ceiling=history_token_ceiling if history_token_ceiling is not None else HISTORY_TOKEN_CEILING,
                    floor=history_token_floor if history_token_floor is not None else HISTORY_TOKEN_FLOOR,
                    model_alias=model_alias,
                ),
                trimmed_the_history,
            ),
        )
        add("context_window_exceeded", graph_node_context_window_exceeded)
    # The profile says whether this KIND of graph has the step; the manifest and the setting say whether this domain, in this deployment, wants it.
    suggests_followups = profile.include_followups and manifest.suggest_followups and FOLLOWUPS_ENABLED
    if suggests_followups:
        # Same: a tool-bound client asked a plain question just answers it.
        add("suggest_followups", make_graph_node_suggest_followups(llm_client, model_alias=model_alias))
    if profile.include_router:
        add("route_model", make_graph_node_route_model())

    # The stage before `agent` is the model router and retrieval, side by side: neither reads what the other writes (`route_model` reads the person's message
    # and writes `model_route`; `retrieve_context` writes `context`, `citations` and `context_anchor_index`), so they are one superstep and `agent` waits for
    # both, where the router's latency (up to ROUTER_TIMEOUT_SECONDS) used to be added to every routed turn before retrieval began. Either may be absent
    # (no router in a subagent's graph; no pre-fetch for a domain that chose "never"), and with both absent a turn goes straight to `agent`.
    before_agent = [name for name, present in (("route_model", profile.include_router), ("retrieve_context", prefetches)) if present]
    retrieval_stage: str | list[str] = before_agent if len(before_agent) > 1 else (before_agent[0] if before_agent else "agent")
    # Where each stage hands off when the optional ones after it are absent.
    after_cache_miss: str | list[str] = "compact_history" if profile.include_compaction else retrieval_stage
    after_moderation: str | list[str] = "check_semantic_cache" if profile.include_cache else after_cache_miss
    # What runs once an answer stands. Neither step needs the other (the cache write reads the
    # answer and its citations; the follow-ups only add `followups`), so they are one parallel
    # superstep: `done` no longer waits for the follow-up LLM call and THEN the cache write's
    # embedding and Redis round trip.
    finish_steps = [
        name
        for name, present in (
            ("suggest_followups", suggests_followups),
            ("write_semantic_cache", profile.include_cache),
        )
        if present
    ]

    def graph_edge_route_after_check_output_bound(state: State) -> Hashable | list[Hashable]:
        route = graph_edge_route_after_check_output(state)
        if route != "finish":
            return route
        return [*finish_steps] if finish_steps else END  # a list display: LangGraph's router type is list[Hashable]

    # The edge counterpart of `add`: a conditional edge is registered under its entry in EDGE_CHANNEL_NAMES, never under its router's
    # Python name, so a checkpoint written before a rename still finds its pending node (`named_edge`).
    def add_edges_after(source: str, router: RouterFn, path_map: dict[Hashable, str] | None = None) -> None:
        builder.add_conditional_edges(source, named_edge(router, EDGE_CHANNEL_NAMES[source]), path_map)

    builder.add_edge(START, "validate_input")
    add_edges_after("validate_input", graph_edge_route_after_validate_input)
    builder.add_edge("reject_input", END)
    builder.add_edge("reject_context", END)

    def then(router: Callable[[State], str], targets: dict[str, str | list[str]]) -> Callable[[State], Hashable | list[Hashable]]:
        """A conditional edge whose answer may be a PAIR of nodes. LangGraph's path map takes a node name for each key and cannot take a list, so this
        returns the target itself (a name, or the router and retrieval together); `identity` below is the map of every name it can return."""

        def route(state: State) -> Hashable | list[Hashable]:
            target = targets[router(state)]
            return [*target] if isinstance(target, list) else target

        return route

    def identity(*targets: str | list[str]) -> dict[Hashable, str]:
        return {name: name for target in targets for name in ([target] if isinstance(target, str) else target)}

    moderation_targets: dict[str, str | list[str]] = {"reject_moderation": "reject_moderation", "check_semantic_cache": after_moderation}
    add_edges_after("moderate_input", then(graph_edge_route_after_moderate_input, moderation_targets), identity(*moderation_targets.values()))
    builder.add_edge("reject_moderation", END)

    if profile.include_cache:
        cache_targets: dict[str, str | list[str]] = {"check_output": "check_output", "compact_history": after_cache_miss}
        add_edges_after("check_semantic_cache", then(graph_edge_route_after_check_semantic_cache, cache_targets), identity(*cache_targets.values()))
    if profile.include_compaction:
        compaction_targets: dict[str, str | list[str]] = {"retrieve_context": retrieval_stage, "context_window_exceeded": "context_window_exceeded"}
        add_edges_after("compact_history", then(graph_edge_route_after_compact_history, compaction_targets), identity(*compaction_targets.values()))
        builder.add_edge("context_window_exceeded", END)
    # `agent` waits for every node of the stage (a barrier: checked on the pinned LangGraph, which waits for ALL the listed nodes).
    if before_agent:
        builder.add_edge(before_agent if len(before_agent) > 1 else before_agent[0], "agent")
    add_edges_after("agent", graph_edge_route_after_agent_bound)
    add_edges_after("human_approval", graph_edge_route_after_human_approval)
    builder.add_edge("tools", "agent")
    builder.add_edge("tool_batch_guard", "agent")

    add_edges_after(
        "check_output",
        graph_edge_route_after_check_output_bound,
        {
            "retry_output": "retry_output",
            "retry_exhausted": "retry_exhausted",
            **{name: name for name in finish_steps},
            END: END,
        },
    )
    builder.add_edge("retry_output", "agent")
    builder.add_edge("retry_exhausted", END)
    builder.add_edge("no_answer", END)
    for name in finish_steps:
        builder.add_edge(name, END)

    compiled = builder.compile(checkpointer=checkpointer or MemorySaver())
    # Not LangGraph API — plain attribute stashes so a caller holding the compiled graph (chiefly
    # tests and scripts) can recover which domain built
    # it and which tools it was given, without changing this function's return type.
    compiled.manifest = manifest  # type: ignore[attr-defined]  # deliberate stash, see comment above
    compiled.tools_by_name = {t.name: t for t in domain_tools}  # type: ignore[attr-defined]  # same
    return compiled


def build_subagent_graph(
    manifest: "AgentManifest",
    domain: "DomainPlugin",
    deps: GraphDeps | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    max_iterations: int | None = None,
    max_tokens_per_turn: int | None = None,
    max_cost_usd_per_turn: float | None = None,
    *,
    emit_no_answer_message: bool = True,
) -> CompiledStateGraph:
    """`build_graph` with `SUBAGENT_PROFILE`, for a NESTED subagent run
    (`app/agent/tools/run_subagent.py`, pattern 46). Parameters mirror `build_graph`'s, minus
    `history_token_ceiling`/`history_token_floor`: there is no `compact_history` to size."""
    return build_graph(
        manifest,
        domain,
        deps,
        checkpointer,
        max_iterations,
        max_tokens_per_turn,
        max_cost_usd_per_turn,
        emit_no_answer_message=emit_no_answer_message,
        profile=SUBAGENT_PROFILE,
    )
