"""`deploy/backup/backup.sh` (PRODUCTION_REVIEW.md R26), run for real against a fake `docker` on PATH.

The script's only outside world is `docker compose exec/run`, so a stand-in that logs every call, prints canned output and can be told to fail runs the real bash: what it
checks is the script's own logic, which is where a backup job goes wrong quietly. It proves: a backup is complete only when its directory has lost the `.partial`
suffix; any failing step fails the run and leaves no complete directory; a corrupt dump is caught by the read-back; the upload happens, with the right prefix, unless
skipped on purpose; and the prune keeps the newest N complete backups and touches nothing else. It does not prove the dumps restore: `make restore-drill` does."""
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "deploy" / "backup" / "backup.sh"

FAKE_DOCKER = r'''#!/usr/bin/env python3
"""A stand-in for `docker compose ...`. Logs argv; FAKE_FAIL=<word> makes any call whose argv contains that word exit 1; FAKE_CORRUPT=<db> makes that db's dump unreadable."""
import os, re, sys
argv = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(" ".join(argv) + "\n")
fail = os.environ.get("FAKE_FAIL")
if fail and fail in " ".join(argv):
    print("fake docker: forced failure", file=sys.stderr)
    sys.exit(1)
rest = argv[argv.index("-f") + 2 :]          # after `compose --project-directory . -f <file>`
verb = rest[0]
if verb == "exec":
    if "pg_dump" in rest:
        db = rest[-1]
        sys.stdout.write("CORRUPT" if os.environ.get("FAKE_CORRUPT") == db else f"PGDMP-{db}")
    elif "pg_restore" in rest:
        data = sys.stdin.read()
        sys.exit(0 if data.startswith("PGDMP-") else 1)
    elif "redis-cli" in rest:
        sys.stdout.write("REDIS0011" + "x" * 20 if os.environ.get("FAKE_REDIS") != "bad" else "NOTRDB")
elif verb == "run":
    mount = next(a for a in rest if re.match(r".+:/backup(:ro)?$", a))
    host_dir = mount.split(":/backup")[0]
    if "qdrant_backup" in " ".join(rest):
        with open(os.path.join(host_dir, "qdrant-docs-abcd1234.snapshot"), "w") as f:
            f.write("SNAPSHOT")
        with open(os.path.join(host_dir, "qdrant-aliases.json"), "w") as f:
            f.write("[]")
'''


@pytest.fixture
def world(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER, encoding="utf-8")
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    root = tmp_path / "backups"
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    return {"bin": bin_dir, "root": root, "log": log}


def _run(world, **env):
    base = {"PATH": f"{world['bin']}:{os.environ['PATH']}", "BACKUP_DIR": str(world["root"]), "FAKE_LOG": str(world["log"]), "HOME": os.environ.get("HOME", "/")}
    return subprocess.run(["bash", str(SCRIPT)], env={**base, **env}, capture_output=True, text=True, cwd="/")


def _complete(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if re.fullmatch(r"\d{8}T\d{6}Z", p.name)) if root.exists() else []


def _calls(world) -> list[str]:
    return world["log"].read_text(encoding="utf-8").splitlines()


def test_a_successful_run_leaves_one_complete_directory_with_every_dump_and_no_partial(world):
    result = _run(world)

    assert result.returncode == 0, result.stderr
    (done,) = _complete(world["root"])
    assert sorted(p.name for p in done.iterdir()) == [
        "appdata.dump", "casdoor.dump", "checkpointer.dump", "litellm.dump", "qdrant-aliases.json", "qdrant-docs-abcd1234.snapshot", "redis.rdb",
    ]
    assert not list(world["root"].glob("*.partial"))
    assert (done / "appdata.dump").read_text(encoding="utf-8") == "PGDMP-appdata" and (done / "casdoor.dump").read_text(encoding="utf-8") == "PGDMP-casdoor"


def test_each_dump_comes_from_the_right_container_as_the_right_role(world):
    _run(world)

    calls = _calls(world)
    for db in ("appdata", "checkpointer", "litellm"):
        assert any(f"exec -T postgres pg_dump -U langfuse --format=custom {db}" in c for c in calls)
    assert any("exec -T casdoor-db pg_dump -U casdoor --format=custom casdoor" in c for c in calls)
    assert any("exec -T redis redis-cli --rdb -" in c for c in calls)


def test_every_dump_is_read_back_before_the_backup_counts(world):
    _run(world)

    restores = [c for c in _calls(world) if "pg_restore --list" in c]
    assert len(restores) == 4, "appdata, checkpointer, litellm and casdoor"


def test_the_upload_runs_from_the_app_image_with_a_prefix_named_for_this_backup_and_after_the_dumps(world):
    _run(world)

    calls = _calls(world)
    uploads = [c for c in calls if "backup_upload" in c]
    assert len(uploads) == 1 and re.search(r"--prefix backups/\d{8}T\d{6}Z$", uploads[0])
    assert ":/backup:ro" in uploads[0], "the upload gets the directory read-only"
    assert calls.index(uploads[0]) > max(i for i, c in enumerate(calls) if "qdrant_backup" in c or "pg_dump" in c)


def test_a_backup_that_skips_the_upload_on_purpose_says_so_and_does_not_upload(world):
    result = _run(world, BACKUP_SKIP_UPLOAD="1")

    assert result.returncode == 0 and "stays on this disk only" in result.stdout
    assert not [c for c in _calls(world) if "backup_upload" in c]


@pytest.mark.parametrize("failing", ["pg_dump -U langfuse --format=custom litellm", "casdoor-db", "redis-cli", "qdrant_backup", "backup_upload"])
def test_any_failing_step_fails_the_run_and_leaves_no_complete_backup(world, failing):
    result = _run(world, FAKE_FAIL=failing)

    assert result.returncode != 0
    assert _complete(world["root"]) == [], "a half-made backup must never look like a whole one"
    assert len(list(world["root"].glob("*.partial"))) == 1


def test_a_failure_stops_the_steps_after_it_instead_of_pressing_on(world):
    _run(world, FAKE_FAIL="pg_dump -U langfuse --format=custom checkpointer")

    calls = _calls(world)
    assert not any("litellm" in c or "backup_upload" in c or "qdrant_backup" in c for c in calls)


def test_a_dump_that_cannot_be_read_back_is_caught(world):
    result = _run(world, FAKE_CORRUPT="checkpointer")

    assert result.returncode != 0 and _complete(world["root"]) == []


def test_a_redis_dump_that_is_not_an_rdb_is_caught(world):
    result = _run(world, FAKE_REDIS="bad")

    assert result.returncode != 0 and "not an RDB" in result.stderr and _complete(world["root"]) == []


@pytest.mark.parametrize(
    "env",
    [{"BACKUP_DIR": "relative/path"}, {"BACKUP_KEEP_LOCAL": "x"}, {"BACKUP_KEEP_LOCAL": "0"}, {"BACKUP_KEEP_LOCAL": "-1"}],
)
def test_a_bad_setting_is_refused_before_anything_is_touched(world, env):
    result = _run(world, **env)

    assert result.returncode == 2 and _calls(world) == [] and not world["root"].exists()


def test_the_prune_keeps_the_newest_n_complete_backups_and_nothing_else(world):
    root = world["root"]
    root.mkdir()
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z", "20260104T000000Z"):
        (root / stamp).mkdir()
        (root / stamp / "appdata.dump").write_text("x", encoding="utf-8")
    (root / "notes.txt").write_text("keep me", encoding="utf-8")          # not a backup name: never matched
    (root / "20260105T000000Z-extra").mkdir()           # looks close, is not the pattern
    (root / "somewhere-else").mkdir()

    result = _run(world, BACKUP_KEEP_LOCAL="2")

    assert result.returncode == 0, result.stderr
    names = sorted(p.name for p in root.iterdir())
    new = [n for n in names if re.fullmatch(r"\d{8}T\d{6}Z", n) and n > "20260104T000000Z"]  # the one this run made: stamped now, after every seeded one
    kept = [n for n in names if re.fullmatch(r"\d{8}T\d{6}Z", n)]
    assert len(kept) == 2 and len(new) == 1, f"the new backup and the newest older one: {kept}"
    assert "20260104T000000Z" in kept and "20260101T000000Z" not in kept and "20260102T000000Z" not in kept
    assert {"notes.txt", "20260105T000000Z-extra", "somewhere-else"} <= set(names)


def test_an_old_partial_directory_from_a_run_that_died_is_cleaned_up_but_a_fresh_one_is_not(world):
    root = world["root"]
    root.mkdir()
    old, fresh = root / "20260101T000000Z.partial", root / "20261114T000000Z.partial"
    old.mkdir()
    fresh.mkdir()
    two_days_ago = 1_700_000_000
    os.utime(old, (two_days_ago, two_days_ago))

    result = _run(world)

    assert result.returncode == 0, result.stderr
    assert not old.exists() and fresh.exists()
