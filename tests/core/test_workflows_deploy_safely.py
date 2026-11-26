"""What reaches production, and what can start the path there (ENGINEERING_PATTERNS.md pattern 106).

`deploy.yml` ran on `workflow_run` with `branches: [main]`. That filter matches the NAME of the branch the triggering run was on, and a pull request
from a fork's own `main` reports exactly that: the deploy would check the fork's commit out with this repository's token and secrets, build it and
publish it as `:latest`. Nothing failed, so only a test keeps the trigger from coming back.
"""
import re
from pathlib import Path

import yaml

from tests.compose_project import load, parts, services

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
PROD = REPO / "deploy" / "prod"
# Triggers that run with this repository's token and secrets on someone else's change.
PRIVILEGED_ON_OUTSIDE_INPUT = {"workflow_run", "pull_request_target"}
# `openapi` runs on pull requests only and `deepeval` never gates on a score; the other two are the release itself.
NOT_A_RELEASE_GATE = {"openapi", "deepeval", "promote", "deploy"}


def _workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _triggers(workflow: dict) -> set[str]:
    on = workflow.get("on", workflow.get(True))  # YAML 1.1 reads a bare `on:` key as the boolean true
    return {on} if isinstance(on, str) else set(on)


def test_no_workflow_runs_privileged_on_an_outside_change():
    found = {path.name: sorted(_triggers(_workflow(path.name)) & PRIVILEGED_ON_OUTSIDE_INPUT) for path in WORKFLOWS.glob("*.yml")}
    assert not {name: triggers for name, triggers in found.items() if triggers}, found


def test_deploy_starts_only_when_ci_calls_it_or_a_maintainer_does():
    assert _triggers(_workflow("deploy.yml")) == {"workflow_call", "workflow_dispatch"}


def test_every_droplet_job_waits_for_the_guard_and_uses_the_production_environment():
    jobs = _workflow("deploy.yml")["jobs"]
    assert jobs["guard"]["if"] == "github.ref == 'refs/heads/main'"
    assert any("compare/$IMAGE_TAG...main" in step.get("run", "") for step in jobs["guard"]["steps"]), "the commit must be checked to be on main"
    for name in set(jobs) - {"guard"}:
        assert jobs[name]["needs"] == "guard", name
        assert jobs[name]["environment"] == "production", name


def test_a_release_waits_for_every_ci_job_and_only_happens_on_main():
    jobs = _workflow("ci.yml")["jobs"]
    missing = set(jobs) - NOT_A_RELEASE_GATE - set(jobs["promote"]["needs"])
    assert not missing, f"jobs a failing run of which would still be released and deployed: {sorted(missing)}"
    condition = jobs["promote"]["if"]
    for required in ("github.event_name == 'push'", "github.ref == 'refs/heads/main'", "'failure'", "'cancelled'", "'skipped'"):
        assert required in condition, f"promote's condition lost {required!r}"
    assert jobs["deploy"]["needs"] == "promote"
    assert "needs.promote.result == 'success'" in jobs["deploy"]["if"]
    assert "vars.DEPLOY_ENABLED == 'true'" in jobs["deploy"]["if"]


def test_the_three_published_images_are_the_ones_promoted_and_checked_before_a_deploy():
    ci = _workflow("ci.yml")["jobs"]
    published = {leg["repository"] for leg in ci["image"]["strategy"]["matrix"]["include"]}
    for name, steps in (("promote", ci["promote"]["steps"]), ("deploy.yml guard", _workflow("deploy.yml")["jobs"]["guard"]["steps"])):
        listed = [m.group(1).split() for step in steps if (m := re.search(r"for image in ([^;]+);", step.get("run", "")))]
        assert listed and set(listed[0]) == published, f"{name} names {listed}, the image job publishes {sorted(published)}"


def test_the_deploy_ships_every_file_the_production_project_reads():
    """A file the entry includes and the sync misses fails `docker compose` on the droplet, after the config was already half replaced."""
    synced = "\n".join(step.get("run", "") for step in _workflow("deploy.yml")["jobs"]["app"]["steps"])
    needed = [*parts(PROD / "docker-compose.yml"), PROD / "docker-compose.yml", PROD / "tools.yml", PROD / "compose.sh", PROD / "release.sh",
              PROD / "crawler-firewall.sh", PROD / "litellm-config.yaml", PROD / "caddy" / "Caddyfile",
              REPO / "deploy" / "shared" / "caddy" / "gateway.caddyfile"]
    missing = sorted(str(path.relative_to(REPO)) for path in set(needed) if not re.search(rf"{re.escape(str(path.relative_to(REPO)))}(\s|$)", synced))
    assert not missing, f"not shipped by deploy.yml: {missing}"


def test_no_production_image_floats_on_latest():
    """An unpinned tag is an upgrade nobody reviewed, applied by whichever deploy happens to pull next. This repo's own images are exempt: their tag is the commit."""
    floating = {}
    for path in PROD.glob("*.yml"):
        if "services" not in (load(path) or {}):
            continue
        for name, service in services(path).items():
            image = service.get("image", "")
            if not image or image.startswith("${") or "@sha256:" in image:
                continue
            last = image.rsplit("/", 1)[-1]
            if ":" not in last or last.endswith(":latest"):
                floating[f"{path.name}:{name}"] = image
    assert not floating, floating
