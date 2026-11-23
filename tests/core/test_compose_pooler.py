"""The connection budget of the pooler (specs/022): the pooled processes plus the direct clients must fit inside Postgres's `max_connections`.

Arithmetic over two compose files that no scanner does. `tests/integration/test_pgbouncer.py` runs the shipped pooler settings against a
real PgBouncer.
"""
import re

from tests.compose_project import REPO, services

DEV, PROD = REPO / "deploy" / "dev" / "docker-compose.yml", REPO / "deploy" / "prod" / "docker-compose.yml"
# Postgres's own default, which the dev file leaves alone and the production file writes down.
DEFAULT_MAX_CONNECTIONS = 100
# What LiteLLM (and Langfuse in dev), the exporters and an operator's psql may hold beside the pooler.
HEADROOM_FOR_DIRECT_CLIENTS = 30


def _env(service: dict) -> dict:
    env = service.get("environment") or {}
    return env if isinstance(env, dict) else dict(item.split("=", 1) for item in env)


def test_the_connection_budget_fits_inside_postgres_and_the_pool_cap_includes_its_reserve():
    for path in (DEV, PROD):
        project = services(path)
        env = _env(project["pgbouncer"])
        cap = int(env["MAX_DB_CONNECTIONS"])
        label = path.parent.name
        assert cap == int(env["DEFAULT_POOL_SIZE"]) + int(env["RESERVE_POOL_SIZE"]), f"{label}: the hard cap is the pool plus its reserve"
        command = " ".join(project["postgres"]["command"])
        match = re.search(r"max_connections=(\d+)", command)
        max_connections = int(match.group(1)) if match else DEFAULT_MAX_CONNECTIONS
        databases = len(env["DATABASE_URLS"].split(","))
        assert databases * cap + HEADROOM_FOR_DIRECT_CLIENTS <= max_connections, (
            f"{label}: {databases} x {cap} pooled + {HEADROOM_FOR_DIRECT_CLIENTS} direct > max_connections={max_connections}"
        )
    assert re.search(r"max_connections=\d+", " ".join(services(PROD)["postgres"]["command"])), "production writes the number down"
