"""`scripts/tools/ci_gate.py`: what may be skipped, and what the one required check (`ci-ok`) accepts."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.tools import ci_gate

REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("paths", [
    ["README.md"], ["docs/ARCHITECTURE.md", "specs/019-x/plan.md"], ["deploy/README.md", "CLAUDE.md"],
    ["packages/billing/docs/RUNBOOK.md"], [".claude/hooks/ci-watch.sh"], [".specify/memory/constitution.md"],
])
def test_documentation_alone_skips_the_slow_jobs(paths):
    assert not ci_gate.changes_code(paths)


@pytest.mark.parametrize("paths", [
    # Markdown the AGENT runs: a change here changes behaviour and must be tested like code.
    ["app/domains/support/skills/refund/SKILL.md"],
    ["app/domains/ops/subagents/triage/AGENT.md"],
    ["app/domains/ops/prompt.md"],
    ["docs/ARCHITECTURE.md", "app/core/config.py"],
    ["requirements-lock.txt"], [".github/workflows/ci.yml"], ["services/ml-service/models.toml"], ["packages/billing/pyproject.toml"],
    [],  # an empty diff is a failed lookup, not a documentation change
])
def test_anything_else_runs_everything(paths):
    assert ci_gate.changes_code(paths)


def _needs(code: str = "true", **results: str) -> dict:
    jobs = {name: {"result": "success", "outputs": {}} for name in ("changes", "lint", "openapi", "test-live", "image", "security")}
    jobs["changes"]["outputs"] = {"code": code}
    for name, result in results.items():
        jobs[name.replace("_", "-")] = {"result": result, "outputs": jobs.get(name.replace("_", "-"), {}).get("outputs", {})}
    return jobs


def test_a_clean_run_passes_on_a_pull_request_and_on_main():
    assert ci_gate.failures(_needs(), "pull_request") == []
    assert ci_gate.failures(_needs(openapi="skipped"), "push") == []


@pytest.mark.parametrize("result", ["failure", "cancelled"])
def test_a_failed_or_cancelled_job_fails_the_run(result):
    assert ci_gate.failures(_needs(lint=result), "pull_request") == [f"lint: {result}"]


def test_a_job_is_only_skipped_where_that_was_decided():
    assert ci_gate.failures(_needs(openapi="skipped"), "pull_request") == ["openapi: skipped"]
    assert ci_gate.failures(_needs(test_live="skipped", image="skipped"), "pull_request") == ["image: skipped", "test-live: skipped"]
    assert ci_gate.failures(_needs(code="false", test_live="skipped", image="skipped"), "pull_request") == []
    assert ci_gate.failures(_needs(code="false", security="skipped"), "pull_request") == ["security: skipped"]


def test_if_the_change_detection_fails_nothing_it_skipped_is_excused():
    needs = _needs(changes="failure", image="skipped")
    needs["changes"]["outputs"] = {}
    assert ci_gate.failures(needs, "pull_request") == ["changes: failure", "image: skipped"]


def _run(*args: str, stdin: str = "", **env: str):
    return subprocess.run(
        [sys.executable, "-I", str(REPO / "scripts" / "tools" / "ci_gate.py"), *args],
        input=stdin, env=env, capture_output=True, text=True, check=False,
    )


def test_it_runs_as_the_workflow_runs_it_with_nothing_installed():
    assert _run("changes", stdin="README.md\ndocs/a.md\n").stdout == "code=false\n"
    assert _run("changes", stdin="app/main.py\n").stdout == "code=true\n"
    assert _run("verdict", NEEDS=json.dumps(_needs()), EVENT="pull_request").returncode == 0
    assert _run("verdict", NEEDS=json.dumps(_needs(lint="failure")), EVENT="pull_request").returncode == 1
    assert _run("verdict", NEEDS="{}", EVENT="push").returncode == 1, "no jobs at all is not a pass"
