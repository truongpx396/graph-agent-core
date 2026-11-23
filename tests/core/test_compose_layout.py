"""References that cross the compose project boundary agree with each other.

The telemetry collectors (exporters, cAdvisor, OTel collector, Promtail) are an overlay on the app's project (`deploy/dev/collectors.yml`);
the observability backends are a project of their own. A service moved between them keeps working only if what used to be an in-network name is
rewritten, and compose reports a dangling `depends_on` only when it is run. Nothing else adds these up.

Each environment's `docker-compose.yml` only `include`s parts, so a part that is added and not listed, or a path that stops resolving inside a
container, is a service nobody deploys or a front door that does not start: both are checked here, because compose finds out when it is run.
"""
import posixpath
import re

import pytest

from app.adapters.inbound.workers.agent_worker import parse_worker_domains
from app.domains.registry import DOMAINS
from tests.compose_project import REPO, parts, services

DEV_DIR, PROD_DIR = REPO / "deploy" / "dev", REPO / "deploy" / "prod"
DEV, COLLECTORS, OBS = DEV_DIR / "docker-compose.yml", DEV_DIR / "collectors.yml", DEV_DIR / "observability.yml"
PROD, OBS_PROD = PROD_DIR / "docker-compose.yml", PROD_DIR / "observability.yml"
SHARED_ROUTING = REPO / "deploy" / "shared" / "caddy" / "gateway.caddy"

# What each environment directory holds that its entry does NOT include, and why: an overlay on the app's project, or a project of its own.
NOT_INCLUDED = {
    DEV_DIR: {"observability.yml", "collectors.yml", "loadtest.yml"},
    PROD_DIR: {"observability.yml"},
}
# Built from source in development (`authlimit:dev`), pulled from the registry in production: the one service whose image differs on purpose.
IMAGE_DIFFERS_BY_DESIGN = {"authlimit"}


def _label(files) -> str:
    return "+".join(f"{path.parent.name}/{path.name}" for path in files)


def test_the_makefile_starts_and_removes_exactly_the_collectors_file_services():
    listed = re.search(r"^COLLECTORS\s*:=\s*(.+)$", (REPO / "Makefile").read_text(encoding="utf-8"), re.MULTILINE)
    assert listed and set(listed.group(1).split()) == set(services(COLLECTORS))


@pytest.mark.parametrize("files", [(DEV,), (DEV, COLLECTORS), (OBS,), (PROD,), (OBS_PROD,)], ids=_label)
def test_every_depends_on_names_a_service_in_the_same_project(files):
    project = services(*files)
    for name, service in project.items():
        depends = service.get("depends_on") or []
        for target in depends if isinstance(depends, list) else depends.keys():
            assert target in project, f"{name} depends_on {target}, which is not in {_label(files)}"


@pytest.mark.parametrize("directory", [DEV_DIR, PROD_DIR], ids=lambda path: path.name)
def test_every_part_in_an_environment_directory_is_included_or_named_as_standalone(directory):
    wired = {part.name for part in parts(directory / "docker-compose.yml")}
    on_disk = {path.name for path in directory.glob("*.yml")}

    assert on_disk == wired | NOT_INCLUDED[directory], (
        f"{directory.name}/: files no entry includes (their services are never deployed): {sorted(on_disk - wired - NOT_INCLUDED[directory])}; "
        f"listed as standalone but missing: {sorted(NOT_INCLUDED[directory] - on_disk)}"
    )


def test_dev_and_production_run_the_same_image_for_a_service_they_share():
    """Production's compose comments promise this about LiteLLM ("the same as dev, a test checks"); nothing did, and a digest bumped in one file only would run
    a different gateway in production than the one `make gateway-check` was run against."""
    dev = services(DEV, COLLECTORS) | services(OBS)
    prod = services(PROD) | services(OBS_PROD)
    differing = {
        name: (dev[name]["image"], prod[name]["image"])
        for name in dev.keys() & prod.keys() - IMAGE_DIFFERS_BY_DESIGN
        if dev[name].get("image") and prod[name].get("image") and dev[name]["image"] != prod[name]["image"]
    }
    assert not differing, f"the same service, a different image in dev and in production: {differing}"


@pytest.mark.parametrize(("entry", "service"), [(DEV, "caddy-gateway"), (PROD, "caddy")], ids=["dev", "prod"])
def test_the_caddyfiles_import_resolves_inside_the_container_to_the_shared_routing(entry, service):
    """The Caddyfile imports the routing by a path relative to itself, and the container mounts each directory where it sits in the repo, so the import
    resolves there as it does on a checkout. A mount that moved without the import (or the reverse) is a Caddy that does not start: the front door is down."""
    caddy = services(entry)[service]
    command = caddy["command"]
    config = command[command.index("--config") + 1]  # a path inside the container
    mounts = {}  # container path -> host directory, for the bind mounts (a named volume has no path on the host)
    for volume in caddy["volumes"]:
        source, target = volume.split(":")[:2]
        if source.startswith((".", "/")):
            mounts[target] = REPO / source

    def on_host(container_path: str):
        for target, source in mounts.items():
            if container_path == target or container_path.startswith(target + "/"):
                return source / posixpath.relpath(container_path, target)
        raise AssertionError(f"{service} reads {container_path}, which no volume mounts")

    entry_file = on_host(config)
    imports = re.findall(r"^import (\S+\.caddy)\s*$", entry_file.read_text(encoding="utf-8"), re.MULTILINE)
    assert imports, f"{entry_file.name} imports no routing file"
    for relative in imports:
        in_container = on_host(posixpath.normpath(posixpath.join(posixpath.dirname(config), relative)))
        assert in_container.resolve() == SHARED_ROUTING.resolve(), f"in the container {relative} reaches {in_container}"
        assert (entry_file.parent / relative).resolve() == SHARED_ROUTING.resolve(), f"on a checkout {relative} reaches another file"


def test_prometheus_scrapes_only_what_its_own_project_or_the_host_publishes():
    """Prometheus is in the observability project: a collector is reached through the host port it publishes, not by service name."""
    import yaml

    obs_services = set(services(OBS))
    config = yaml.safe_load((REPO / "observability" / "prometheus" / "prometheus.yml").read_text(encoding="utf-8"))
    hosts = {t.split(":")[0] for job in config["scrape_configs"] for sc in job["static_configs"] for t in sc["targets"]}
    hosts |= {t.split(":")[0] for am in config["alerting"]["alertmanagers"] for sc in am["static_configs"] for t in sc["targets"]}
    assert hosts <= obs_services | {"localhost", "host.docker.internal"}, f"scrape targets not reachable from {OBS.name}: {sorted(hosts - obs_services)}"


def test_promtail_pushes_to_loki_through_the_host_and_keeps_only_services_that_exist():
    import yaml

    config = yaml.safe_load((REPO / "observability" / "promtail" / "promtail-config.yaml").read_text(encoding="utf-8"))
    assert "loki" not in config["clients"][0]["url"].split("/")[2].split(":")[0], "Promtail is not in Loki's project"
    keep = next(r for r in config["scrape_configs"][0]["relabel_configs"] if r.get("action") == "keep")
    allowed = set(re.fullmatch(r"\^\((.+)\)\$", keep["regex"]).group(1).split("|"))
    unknown = allowed - set(services(DEV, COLLECTORS))
    assert not unknown, f"promtail's allowlist names services no compose file defines: {sorted(unknown)}"


@pytest.mark.parametrize("path", [DEV, PROD], ids=lambda p: p.parent.name)
def test_the_domains_a_compose_worker_serves_by_default_are_ones_the_registry_has(path):
    spec = services(path)["agent-worker"]["environment"]["AGENT_WORKER_DOMAINS"]
    default = re.fullmatch(r"\$\{AGENT_WORKER_DOMAINS:-(.*)\}", spec).group(1)
    assert parse_worker_domains(default, sorted(DOMAINS))
