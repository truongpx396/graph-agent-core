"""Config-first multi-domain composition layer (pattern 23): the ONE StateGraph topology
(`app/agent/graph/build.py`) is adapted to a domain by swapping an `AgentManifest` (config)
plus a `DomainPlugin` (tools/capabilities/policy), never by forking `build_graph()` or
branching on a domain name.

- `AgentManifest` — the YAML/JSON-able part: name, system prompt, which of the plugin's tools
  this deployment exposes.
- `DomainPlugin` — the CODE a manifest can't express: tool implementations, capability
  declarations (read_only/mutating/outward, see `app/agent/tools/builtin.py::SHARED_TOOL_CAPABILITIES`
  and each domain's `tools.py`), and the `Policy` they enforce internally. `build_graph()` never
  calls `.policy()` itself — enforcement lives at the tool-implementation boundary (pattern 17);
  it exists so a plugin can assert/expose its own Policy.

These two types are the agent's plugin port: what the graph asks of a domain, stated so the agent never names a concrete one. The
concrete domains are in `app/domains/<name>/domain.py`; Ecorp is one of them, with no special standing in the graph. See
`tests/agent/test_manifest.py` for a second domain proving the same graph runs with different tools/prompt/Policy.

`DomainCatalog` is the other half: how the runtime gets a domain BY NAME when its caller named none. The adapter is
`app/domains/registry.py`, the one module that names every concrete domain, bound at the composition root.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol

from app.core.security import Policy
from app.ports.slot import Slot


class DomainPlugin(Protocol):
    """The CODE half of a domain. See this module's docstring for why
    `policy()` exists but is never called by `build_graph()` itself."""

    def tools(self) -> list[Any]:
        """This domain's full tool universe (LangChain `@tool`-decorated
        callables) — `AgentManifest.allowed_tools` picks the subset a
        given deployment actually exposes."""
        ...

    def tool_capabilities(self) -> dict[str, str]:
        """Maps each tool name to `"read_only" | "mutating" | "outward"` —
        see `app/agent/tools/builtin.py::SHARED_TOOL_CAPABILITIES`'s comment for the mandatory
        human_approval gate this feeds (`should_continue`, unchanged
        regardless of domain)."""
        ...

    def policy(self) -> Policy:
        """This domain's `Policy` — informational (see module docstring):
        the plugin's own `tools()` are expected to already enforce it."""
        ...


@dataclass(frozen=True)
class AgentManifest:
    """The CONFIG half of a domain. Frozen so a manifest can't be mutated
    out from under a graph already built with it — the same "stamped once,
    read-only from there on" discipline `State["ctx"]` already follows
    (see `app/agent/graph/state.py`'s `State` docstring).
    """

    name: str
    system_prompt: str
    # Subset of the plugin's tool names to actually expose to this
    # deployment's LLM. Empty/omitted means "everything the plugin
    # offers" — a plugin author doesn't have to enumerate every tool name
    # twice just to expose all of them.
    allowed_tools: tuple[str, ...] = ()
    # Whether an answer that cites something is followed by a second model call that suggests follow-up questions (`suggest_followups`, pattern 27).
    # It runs in parallel with the cache write, but `done` waits for both, so it costs a model call and the time of one on every cited answer. A domain
    # whose answers usually end in an action rather than a reading list leaves it off (PRODUCTION_REVIEW.md R30); the node is then not in its graph at all.
    suggest_followups: bool = True
    # Whether `retrieve_context` runs before the model on every turn: a document search and a memory search, each with an embedding, a sparse vector, a
    # Qdrant query and a rerank, for "hi", for "thanks" and for "log an incident" in a domain whose tools do not read documents (PRODUCTION_REVIEW.md R29).
    # With "never" the node is not in the graph and the model sees no pre-fetched documents or memories: a domain that still needs documents must have
    # the `search_docs` tool and its prompt must tell the model to call it. "always" is the shipped behaviour, for every domain: switching one over
    # changes what its answers are grounded on, which `make eval` and the domain's promptfoo suite judge, not a unit test.
    prefetch: Literal["always", "never"] = "always"


class DomainCatalog(Protocol):
    def resolve(self, name: str) -> tuple[AgentManifest, DomainPlugin]:
        """The manifest and plugin for `name`. Raises `ValueError`, listing the known names, for an unknown one: a typo'd domain fails loud
        instead of silently falling back to another."""
        ...

    def names(self) -> list[str]:
        """Every domain this process can serve, sorted."""
        ...


domain_catalog: Slot[DomainCatalog] = Slot("domain_catalog", DomainCatalog)
