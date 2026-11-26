"""`deploy/prod/release.sh`: how a published release reaches the app droplet, by CI or by hand. Run for real with a fake `docker`, `curl` and `timeout`."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
OLD, NEW = "a" * 40, "b" * 40
PLAIN = "compose --project-directory . -f deploy/prod/docker-compose.yml"
FAKES = {
    # Logs the tag compose would interpolate with each call; `run` (the billing check) answers FAKE_MIGRATE.
    "docker": 'printf "%s | %s\\n" "$IMAGE_TAG" "$*" >> "$FAKE_LOG"\ncase "$*" in *" run "*) exit "${FAKE_MIGRATE:-0}";; esac\n',
    "curl": "exit 0\n",
    # `timeout N cmd...`: the front door answers (run the command) or never does (exit as coreutils does).
    "timeout": '[ "${FAKE_READY:-1}" = 1 ] || exit 124\nshift\nexec "$@"\n',
}


@pytest.fixture
def droplet(tmp_path):
    (tmp_path / "deploy" / "prod").mkdir(parents=True)
    for name in ("compose.sh", "release.sh"):
        shutil.copy(REPO / "deploy" / "prod" / name, tmp_path / "deploy" / "prod" / name)
        (tmp_path / "deploy" / "prod" / name).chmod(0o755)
    (tmp_path / "bin").mkdir()
    for name, body in FAKES.items():
        (tmp_path / "bin" / name).write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
        (tmp_path / "bin" / name).chmod(0o755)
    (tmp_path / ".env").write_text("API_DOMAIN=api.example.test\n", encoding="utf-8")
    (tmp_path / ".image-tag").write_text(f"{OLD}\n", encoding="utf-8")
    return tmp_path


def release(droplet, tag: str, **env: str):
    log = droplet / "log.txt"
    clean = {key: value for key, value in os.environ.items() if key != "IMAGE_TAG"}
    result = subprocess.run(
        ["bash", str(droplet / "deploy" / "prod" / "release.sh"), tag],
        env={**clean, "PATH": f"{droplet / 'bin'}:{os.environ['PATH']}", "FAKE_LOG": str(log), **env},
        capture_output=True, text=True, cwd="/",
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls, (droplet / ".image-tag").read_text(encoding="utf-8").strip()


def test_a_release_pulls_starts_records_the_tag_and_prunes_only_once_ready(droplet):
    result, calls, recorded = release(droplet, NEW)
    assert result.returncode == 0, result.stderr
    assert calls == [f"{NEW} | {PLAIN} pull", f"{NEW} | {PLAIN} up -d --remove-orphans", f"{NEW} | image prune -af --filter until=168h"]
    assert recorded == NEW


@pytest.mark.parametrize("tag", ["", "latest", "b" * 39, "B" * 40, f"{NEW};reboot"])
def test_anything_but_a_full_commit_sha_is_refused_before_docker_is_called(droplet, tag):
    result, calls, recorded = release(droplet, tag)
    assert result.returncode == 2 and calls == [] and recorded == OLD


def test_with_credits_priced_a_schema_that_is_not_current_stops_before_anything_restarts(droplet):
    (droplet / ".env").write_text("API_DOMAIN=api.example.test\nCREDITS_PER_USD=1000\n", encoding="utf-8")
    result, calls, recorded = release(droplet, NEW, FAKE_MIGRATE="1")
    assert result.returncode == 1 and "billing schema is not current" in result.stderr
    assert calls[-1].endswith("run --rm --no-deps api python -m credit_billing.cli.migrate --check")
    assert not any(" up " in call for call in calls) and recorded == OLD


@pytest.mark.parametrize("env_line", ["CREDITS_PER_USD=\n", "# CREDITS_PER_USD=1000\n", ""])
def test_without_a_price_for_credits_there_is_no_schema_check(droplet, env_line):
    (droplet / ".env").write_text(f"API_DOMAIN=api.example.test\n{env_line}", encoding="utf-8")
    result, calls, _ = release(droplet, NEW)
    assert result.returncode == 0 and not any("migrate" in call for call in calls)


def test_a_release_that_never_answers_fails_names_the_way_back_and_keeps_the_old_images(droplet):
    result, calls, recorded = release(droplet, NEW, FAKE_READY="0")
    assert result.returncode == 1
    assert f"image_tag={OLD}" in result.stderr
    assert not any("prune" in call for call in calls), "the images a rollback needs must still be there"
    assert recorded == NEW, "what was started is what a later `up` must keep running"


def test_without_the_api_domain_it_says_why_it_cannot_check_the_release(droplet):
    (droplet / ".env").write_text("POSTGRES_PASSWORD=x\n", encoding="utf-8")
    result, calls, _ = release(droplet, NEW)
    assert result.returncode == 1 and "API_DOMAIN is not set" in result.stderr
    assert not any("prune" in call for call in calls)

