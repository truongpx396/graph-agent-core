"""Hermetic tests for the answer cache's tenant/principal scoping in
app/adapters/outbound/redis/semantic_cache.py — the part that is easiest to get wrong and
costs most when wrong (a cached answer can carry citations to one principal's
own memories).

Until now the `_escape_tag` guarantee — the fix for "tenant `other-co` raised a
RediSearch syntax error" (GRAPH_PATTERNS.md pattern 22) — was exercised only
*incidentally*, by a real-Redis concurrency test that happens to use hyphenated
tenant names. These pin it by name, without Redis, and cover the principal axis,
which no test touched. (Escaping of the *full* syntax-character set is tested
with its own fix, in test_semantic_cache_tag_escaping.py.)
"""
import pytest

from app.adapters.outbound.redis import semantic_cache


class TestEscapeTag:
    def test_a_hyphenated_tenant_is_escaped(self):
        assert semantic_cache._escape_tag("other-co") == r"other\-co"

    def test_ordinary_identifiers_pass_through_unchanged(self):
        assert semantic_cache._escape_tag("ecorp_2") == "ecorp_2"


class _RecordingClient:
    def __init__(self):
        self.queries = []

    def ft(self, _index):
        return self

    async def search(self, query, query_params=None):
        self.queries.append(query.query_string())
        return type("Result", (), {"docs": []})()


@pytest.fixture
def client(monkeypatch):
    recording = _RecordingClient()

    async def no_index(_client):
        return semantic_cache._Namespace("idx:test", "cache:test:")

    async def fake_embed(_text):
        return [0.0, 0.0]

    monkeypatch.setattr(semantic_cache, "_get_client", lambda: recording)
    monkeypatch.setattr(semantic_cache, "_ensure_index", no_index)
    monkeypatch.setattr(semantic_cache, "embed_query", fake_embed)
    return recording


class TestGetQueryIsScopedAndEscaped:
    async def test_the_query_restricts_to_both_tenant_and_principal(self, client):
        ctx = {"tenant": "acme", "principal": "alice", "claims": {}}

        await semantic_cache.get(ctx, "what is the refund policy?", domain="ecorp")

        assert client.queries == ["(@tenant:{acme} @principal:{alice} @domain:{ecorp})=>[KNN 1 @embedding $vec AS dist]"]

    async def test_syntax_characters_in_either_axis_are_escaped(self, client):
        """`other-co` raised a syntax error before the fix; the principal axis
        was never covered. Both go through `_escape_tag`."""
        ctx = {"tenant": "other-co.eu", "principal": "a@b c", "claims": {}}

        await semantic_cache.get(ctx, "hello", domain="ecorp")

        assert client.queries == [
            r"(@tenant:{other\-co\.eu} @principal:{a\@b\ c} @domain:{ecorp})=>[KNN 1 @embedding $vec AS dist]"
        ]

    @pytest.mark.parametrize(
        "ctx",
        [None, {}, {"tenant": "acme", "principal": "", "claims": {}}, {"tenant": "", "principal": "p", "claims": {}}],
        ids=["no-ctx", "empty-ctx", "no-principal", "no-tenant"],
    )
    async def test_an_incomplete_ctx_is_a_miss_that_never_queries(self, client, ctx):
        assert await semantic_cache.get(ctx, "hello", domain="ecorp") is None
        assert client.queries == []


class TestTheDomainIsPartOfTheKey:
    """The cache scoped an entry by tenant and person only, so the same person asking the same question in another domain (another prompt, other
    tools) was handed the first domain's answer (PRODUCTION_REVIEW.md R23)."""

    async def test_the_lookup_is_restricted_to_the_domain_it_is_asked_in(self, client):
        ctx = {"tenant": "acme", "principal": "alice", "claims": {}}

        await semantic_cache.get(ctx, "hello", domain="support")
        await semantic_cache.get(ctx, "hello", domain="ops")

        assert [q.split("=>")[0] for q in client.queries] == [
            "(@tenant:{acme} @principal:{alice} @domain:{support})",
            "(@tenant:{acme} @principal:{alice} @domain:{ops})",
        ]

    async def test_a_domain_cannot_widen_the_filter_either(self, client):
        ctx = {"tenant": "acme", "principal": "alice", "claims": {}}

        await semantic_cache.get(ctx, "hello", domain="a}|{b")

        assert client.queries[0].startswith(r"(@tenant:{acme} @principal:{alice} @domain:{a\}\|\{b})")

    async def test_the_port_adapter_passes_the_domain_through(self, client):
        ctx = {"tenant": "acme", "principal": "alice", "claims": {}}

        await semantic_cache.RedisAnswerCache().get(ctx, "hello", domain="sales")

        assert client.queries[0].startswith("(@tenant:{acme} @principal:{alice} @domain:{sales})")


class TestWhichRedisTheCacheUses:
    """PRODUCTION_REVIEW.md R15: a cache wants `allkeys-lru` and the queue, the locks and the front door's revocation list must never be evicted, so the production
    stack gives the cache its own Redis. The cache opens its client against `SEMANTIC_CACHE_REDIS_URL`, which is `REDIS_URL` when it is not set."""

    def test_the_cache_connects_to_its_own_url_not_the_queues(self, monkeypatch):
        seen = []
        monkeypatch.setattr(semantic_cache, "_client", None)
        monkeypatch.setattr(semantic_cache, "SEMANTIC_CACHE_REDIS_URL", "redis://redis-cache:6379")
        monkeypatch.setattr(semantic_cache.redis.Redis, "from_url", classmethod(lambda cls, url, **kw: seen.append(url) or object()))

        semantic_cache._get_client()

        assert seen == ["redis://redis-cache:6379"]

    @pytest.mark.parametrize(
        ("env", "expected"),
        [
            ({"REDIS_URL": "redis://queue:6379"}, "redis://queue:6379"),  # unset: the main Redis, so development and older deployments are unchanged
            ({"REDIS_URL": "redis://queue:6379", "SEMANTIC_CACHE_REDIS_URL": ""}, "redis://queue:6379"),  # blank is unset
            ({"REDIS_URL": "redis://queue:6379", "SEMANTIC_CACHE_REDIS_URL": "redis://cache:6379"}, "redis://cache:6379"),
        ],
        ids=["unset", "blank", "set"],
    )
    def test_the_url_the_adapter_reads_is_the_setting_and_falls_back_to_the_main_redis(self, env, expected, tmp_path):
        """A fresh interpreter, because the constant is read from the environment once, at import; run from an empty directory so no developer's `.env` file is read."""
        import os
        import subprocess
        import sys

        # The child gets this process's import path, not just the repo: the billing package is found by pytest's `pythonpath` setting, not installed, so a child
        # started with only the repo on its path cannot import `app.core.config` (CI, where nothing is installed in editable mode). `-S` below makes a local run behave like CI.
        import_path = os.pathsep.join(p for p in sys.path if p)
        clean = {k: v for k, v in os.environ.items() if k not in {"REDIS_URL", "SEMANTIC_CACHE_REDIS_URL"}}
        out = subprocess.run(
            [sys.executable, "-S", "-c", "from app.core.config import SEMANTIC_CACHE_REDIS_URL as u; print(u)"],  # -S: no `.pth` files, so nothing installed locally can hide a missing path
            env={**clean, "PYTHONPATH": import_path, **env}, capture_output=True, text=True, check=True, cwd=tmp_path,
        )

        assert out.stdout.strip() == expected
