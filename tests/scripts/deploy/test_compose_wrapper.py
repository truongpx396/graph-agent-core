"""`deploy/prod/compose.sh`: the one way CI and the operator run compose on the production project.

Every deploy now goes through it, so its default path must be EXACTLY the command CI ran before it existed; and with the crawler enabled it must never start anything while the
firewall is missing, nor run a command without the overlay (an `up --remove-orphans` without it would remove the crawler). Run for real with a fake `docker` that logs its argv."""
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PLAIN = ["compose", "--project-directory", ".", "-f", "deploy/prod/docker-compose.yml"]
OVERLAY = [*PLAIN, "-f", "deploy/prod/tools.yml"]


@pytest.fixture
def site(tmp_path):
    """A copy of the layout the script expects: <root>/deploy/prod/{compose.sh, crawler-firewall.sh} and <root>/.env."""
    (tmp_path / "deploy" / "prod").mkdir(parents=True)
    shutil.copy(REPO / "deploy" / "prod" / "compose.sh", tmp_path / "deploy" / "prod" / "compose.sh")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$FAKE_LOG"\n', encoding="utf-8")
    (bin_dir / "docker").chmod(0o755)
    return tmp_path


def firewall(site, *, installed: bool):
    path = site / "deploy" / "prod" / "crawler-firewall.sh"
    path.write_text(f"#!/bin/sh\n[ \"$1\" = status ] && exit {0 if installed else 1}\nexit 2\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def compose(site, *args, env_file: str | None = None):
    if env_file is not None:
        (site / ".env").write_text(env_file, encoding="utf-8")
    log = site / "log.txt"
    result = subprocess.run(
        ["bash", str(site / "deploy" / "prod" / "compose.sh"), *args],
        env={**os.environ, "PATH": f"{site / 'bin'}:{os.environ['PATH']}", "FAKE_LOG": str(log)},
        capture_output=True, text=True, cwd="/",
    )
    return result, (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])


def test_by_default_it_runs_exactly_the_command_ci_ran_before_it_existed(site):
    result, calls = compose(site, "up", "-d", "--remove-orphans", env_file="POSTGRES_PASSWORD=x\n")
    assert result.returncode == 0
    assert calls == [" ".join([*PLAIN, "up", "-d", "--remove-orphans"])]


def test_with_no_env_file_at_all_it_is_still_the_plain_command(site):
    result, calls = compose(site, "pull")
    assert result.returncode == 0 and calls == [" ".join([*PLAIN, "pull"])]


@pytest.mark.parametrize("line", ["CRAWLER_ENABLED=false\n", "# CRAWLER_ENABLED=true\n", "CRAWLER_ENABLED=true # yes\n", " CRAWLER_ENABLED=true\n"])
def test_only_an_exact_line_enables_the_crawler(site, line):
    firewall(site, installed=True)
    _, calls = compose(site, "ps", env_file=line)
    assert calls == [" ".join([*PLAIN, "ps"])], "a commented, spaced or annotated line must not switch a security-relevant overlay on"


def test_enabled_with_the_firewall_in_place_adds_the_overlay_to_every_command(site):
    firewall(site, installed=True)
    for verb in ("pull", "up", "ps"):
        _, calls = compose(site, verb, env_file="CRAWLER_ENABLED=true\n")
        assert calls[-1] == " ".join([*OVERLAY, verb])


@pytest.mark.parametrize("verb", ["up", "start", "restart", "create", "run"])
def test_enabled_without_the_firewall_it_refuses_to_start_anything_and_calls_docker_not_at_all(site, verb):
    firewall(site, installed=False)
    result, calls = compose(site, verb, env_file="CRAWLER_ENABLED=true\n")
    assert result.returncode == 1 and calls == []
    assert "firewall is not installed" in result.stderr and "crawler-firewall.sh install" in result.stderr


@pytest.mark.parametrize("verb", ["ps", "logs", "pull", "down", "exec"])
def test_enabled_without_the_firewall_a_command_that_starts_nothing_still_runs(site, verb):
    firewall(site, installed=False)
    result, calls = compose(site, verb, env_file="CRAWLER_ENABLED=true\n")
    assert result.returncode == 0 and calls == [" ".join([*OVERLAY, verb])], "an operator must still be able to look, and to stop it"
