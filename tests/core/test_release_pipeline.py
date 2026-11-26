"""What CI, the deploy and the compose files must agree on, which nothing else adds up (ENGINEERING_PATTERNS.md patterns 106 and 107).

What a single workflow file says on its own (a dangerous trigger, an unpinned action, a missing permission) is zizmor's and actionlint's, in
`make lint-workflows`.
"""
import re
from pathlib import Path

import yaml

from tests.compose_project import parts

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"
PROD = REPO / "deploy" / "prod"
# `ci-ok` is the gate itself and the other two are the release.
NOT_BEHIND_THE_GATE = {"ci-ok", "promote", "deploy"}


def _jobs(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))["jobs"]


def test_the_one_required_check_covers_every_job_and_runs_even_when_they_fail():
    """The branch ruleset requires `ci-ok` alone: a job left out of its `needs` can be red at merge and still be released."""
    jobs = _jobs("ci.yml")
    missing = set(jobs) - NOT_BEHIND_THE_GATE - set(jobs["ci-ok"]["needs"])
    assert not missing, f"jobs whose failure would stop neither a merge nor a release: {sorted(missing)}"
    assert jobs["ci-ok"]["if"] == "${{ always() }}", "a required check that is skipped counts as passed"


def test_the_images_published_are_the_ones_promoted_checked_before_a_deploy_and_rescanned():
    published = {leg["repository"] for leg in _jobs("ci.yml")["image"]["strategy"]["matrix"]["include"]}
    for name, steps in (("promote", _jobs("ci.yml")["promote"]["steps"]), ("deploy.yml guard", _jobs("deploy.yml")["guard"]["steps"])):
        listed = [m.group(1).split() for step in steps if (m := re.search(r"for image in ([^;]+);", step.get("run", "")))]
        assert listed and set(listed[0]) == published, f"{name} names {listed}, the image job publishes {sorted(published)}"
    assert set(_jobs("security.yml")["published-images"]["strategy"]["matrix"]["repository"]) == published


def test_the_deploy_ships_every_file_the_production_project_reads():
    """A file the entry includes and the sync misses fails `docker compose` on the droplet, after the config was already half replaced."""
    synced = "\n".join(step.get("run", "") for step in _jobs("deploy.yml")["app"]["steps"])
    needed = [*parts(PROD / "docker-compose.yml"), PROD / "tools.yml", PROD / "compose.sh", PROD / "release.sh", PROD / "crawler-firewall.sh",
              PROD / "litellm-config.yaml", PROD / "caddy" / "Caddyfile", REPO / "deploy" / "shared" / "caddy" / "gateway.caddyfile"]
    missing = sorted(str(path.relative_to(REPO)) for path in set(needed) if not re.search(rf"{re.escape(str(path.relative_to(REPO)))}(\s|$)", synced))
    assert not missing, f"not shipped by deploy.yml: {missing}"
