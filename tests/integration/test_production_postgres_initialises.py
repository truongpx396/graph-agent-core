"""A NEW production Postgres volume initialises (PRODUCTION_REVIEW.md R26).

`deploy/prod/infra.yml` set `POSTGRES_DB: appdata`, so the image created `appdata` itself and then `postgres-init/02-appdata.sql` ran `CREATE DATABASE appdata`: "already
exists", the init aborted, and the container exited with status 3. An existing volume never runs the init again, so nothing showed it; a new droplet, or a restore onto a
rebuilt one, could not start Postgres. The test helpers did not see it either: they start the DEVELOPMENT layout (`POSTGRES_DB=langfuse`).

This starts the production service as the production compose file defines it (its image, its environment, its command) on an empty volume with the repository's own init
scripts, and checks it comes up and holds the databases the application needs. Self-skips (never fails) without Docker."""
import time
from pathlib import Path

import psycopg
import pytest
import yaml

from tests.containers import _require_docker

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[2]


def _production_postgres() -> dict:
    return yaml.safe_load((REPO / "deploy" / "prod" / "infra.yml").read_text(encoding="utf-8"))["services"]["postgres"]


def test_a_new_volume_initialises_and_holds_every_database_the_application_needs():
    _require_docker("the production Postgres")
    from testcontainers.core.container import DockerContainer

    service = _production_postgres()
    environment = {**service["environment"], "POSTGRES_PASSWORD": "drill"}
    container = DockerContainer(service["image"]).with_exposed_ports(5432).with_volume_mapping(str(REPO / "postgres-init"), "/docker-entrypoint-initdb.d", mode="ro")
    for key, value in environment.items():
        container = container.with_env(key, str(value))
    container = container.with_command(" ".join(service["command"][1:]))
    container.start()
    try:
        url = f"postgresql://langfuse:drill@{container.get_container_host_ip()}:{int(container.get_exposed_port(5432))}/postgres"
        names: set[str] = set()
        for _ in range(90):
            status = container.get_wrapped_container()
            status.reload()
            assert status.status == "running", f"Postgres is {status.status} (exit {status.attrs['State']['ExitCode']}): a new volume did not initialise"
            try:
                with psycopg.connect(url, connect_timeout=2) as conn:
                    names = {row[0] for row in conn.execute("SELECT datname FROM pg_database WHERE NOT datistemplate").fetchall()}
                if {"appdata", "checkpointer", "litellm"} <= names:
                    break
            except psycopg.OperationalError:
                pass  # the server restarts once after the init scripts
            time.sleep(1)
        assert {"appdata", "checkpointer", "litellm"} <= names, f"databases after initialising: {sorted(names)}"
        assert "langfuse" not in names, "the production compose file promises there is no `langfuse` database, just the role"
    finally:
        container.stop()
