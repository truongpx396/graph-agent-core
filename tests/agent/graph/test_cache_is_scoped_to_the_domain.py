"""A graph's cache lookups and writes carry the domain the graph was built for (PRODUCTION_REVIEW.md R23).

The answer cache scoped an entry by tenant and person only, so the same person asking the same question in another domain (another prompt, other
tools) was handed the first domain's answer. `build_graph` binds the default cache to `manifest.name`, the one place that knows it: the runtime knows
it too but imports the builder.
"""
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from app.agent.graph import build as graph_build
from app.agent.graph.build import GraphDeps
from app.domains.sales.domain import SALES_DOMAIN_PLUGIN, SALES_MANIFEST
from app.domains.support.domain import SUPPORT_DOMAIN_PLUGIN, SUPPORT_MANIFEST
from tests.agent.fakes import cfg
from tests.ecorp import build_graph


async def _run_one_turn(graph):
    await graph.ainvoke({"messages": [("user", "How do refunds work for an annual plan?")]}, config=cfg())


def _record(monkeypatch):
    asked, written = [], []

    async def get(ctx, query, *, domain):
        asked.append(domain)

    async def set_(ctx, query, answer, citations, *, domain):
        written.append(domain)

    monkeypatch.setattr(graph_build, "default_cache_get", get)
    monkeypatch.setattr(graph_build, "default_cache_set", set_)
    return asked, written


def _llm():
    return GenericFakeChatModel(messages=iter([AIMessage(content="Annual plans are refunded pro rata within thirty days of purchase.")]))


async def test_the_default_cache_is_asked_and_written_under_the_graphs_own_domain(monkeypatch):
    asked, written = _record(monkeypatch)

    graph = build_graph(GraphDeps(llm=_llm()))
    await _run_one_turn(graph)

    scope = f"ecorp@{graph.prompt_version}"
    assert asked == [scope] and written == [scope]


async def test_another_domains_graph_uses_its_own_name(monkeypatch):
    asked, written = _record(monkeypatch)

    await _run_one_turn(build_graph(GraphDeps(llm=_llm()), manifest=SUPPORT_MANIFEST, domain=SUPPORT_DOMAIN_PLUGIN))
    await _run_one_turn(build_graph(GraphDeps(llm=_llm()), manifest=SALES_MANIFEST, domain=SALES_DOMAIN_PLUGIN))

    assert [scope.split("@")[0] for scope in asked] == ["support", "sales"] and written == asked


async def test_a_cache_a_test_injects_is_used_as_given_with_no_domain_forced_on_it(monkeypatch):
    calls = []

    async def get(ctx, query):
        calls.append(("get", query))

    async def set_(ctx, query, answer, citations):
        calls.append(("set", query))

    await _run_one_turn(build_graph(GraphDeps(llm=_llm(), cache_get=get, cache_set=set_)))

    assert [kind for kind, _ in calls] == ["get", "set"]
