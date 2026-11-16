"""`scripts/tools/backup_upload.py` (PRODUCTION_REVIEW.md R26): a backup is complete if and only if its manifest exists, and `--latest` is the check for a backup
job that stopped running."""
import hashlib
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.tools import backup_upload


class _Client:
    """The few calls of the MinIO client the tool makes. `fail_on` names a file whose upload raises."""

    def __init__(self, existing=(), fail_on=None):
        self.puts: list[str] = []
        self.bodies: dict[str, bytes] = {}
        self.fail_on = fail_on
        self.existing = list(existing)

    def bucket_exists(self, name):
        return True

    def fput_object(self, bucket, key, path, **kw):
        if self.fail_on and key.endswith(self.fail_on):
            raise ConnectionError("the object store went away")
        self.puts.append(key)

    def put_object(self, bucket, key, data, length, content_type=None, **kw):
        self.puts.append(key)
        self.bodies[key] = data.read()

    def list_objects(self, bucket, prefix=None, recursive=False, **kw):
        return [SimpleNamespace(object_name=name, last_modified=when) for name, when in self.existing if name.startswith(prefix or "")]

    # a tiny object store for the download side: `stored` maps key -> bytes
    stored: dict[str, bytes] = {}

    def get_object(self, bucket, key, **kw):
        if key not in self.stored:
            raise KeyError(key)
        data = self.stored[key]
        return SimpleNamespace(read=lambda: data, close=lambda: None, release_conn=lambda: None)

    def fget_object(self, bucket, key, path, **kw):
        Path(path).write_bytes(self.stored[key])


@pytest.fixture(autouse=True)
def bucket(monkeypatch):
    monkeypatch.setattr(backup_upload.object_store, "ensure_bucket", lambda client=None: None)


@pytest.fixture
def directory(tmp_path):
    (tmp_path / "appdata.dump").write_bytes(b"A" * 10)
    (tmp_path / "qdrant-docs.snapshot").write_bytes(b"Q" * 25)
    return tmp_path


def test_every_file_is_uploaded_and_the_manifest_goes_last(directory):
    client = _Client()

    backup_upload.upload(directory, "backups/20261115T000000Z", client=client)

    assert client.puts == ["backups/20261115T000000Z/appdata.dump", "backups/20261115T000000Z/qdrant-docs.snapshot", "backups/20261115T000000Z/manifest.json"]


def test_the_manifest_lists_each_files_size_and_sha256(directory):
    client = _Client()

    manifest = backup_upload.upload(directory, "backups/x", client=client)

    assert manifest["files"] == {
        "appdata.dump": {"bytes": 10, "sha256": hashlib.sha256(b"A" * 10).hexdigest()},
        "qdrant-docs.snapshot": {"bytes": 25, "sha256": hashlib.sha256(b"Q" * 25).hexdigest()},
    }
    assert json.loads(client.bodies["backups/x/manifest.json"]) == manifest
    assert datetime.fromisoformat(manifest["created_at"]).tzinfo is not None


def test_a_failed_upload_leaves_no_manifest_so_the_partial_backup_does_not_look_complete(directory):
    client = _Client(fail_on="qdrant-docs.snapshot")

    with pytest.raises(ConnectionError):
        backup_upload.upload(directory, "backups/x", client=client)

    assert "backups/x/manifest.json" not in client.puts


@pytest.mark.parametrize("prefix", ["", "x", "backup/x", "backups/x/", "uploads/backups/x"])
def test_a_prefix_outside_backups_is_refused_so_a_tenants_uploads_are_never_overwritten(directory, prefix):
    with pytest.raises(ValueError):
        backup_upload.upload(directory, prefix, client=_Client())


def test_an_empty_directory_is_an_error_not_an_empty_backup(tmp_path):
    with pytest.raises(FileNotFoundError):
        backup_upload.upload(tmp_path, "backups/x", client=_Client())


NOW = datetime.now(UTC)


def test_the_newest_complete_backup_is_the_newest_manifest_and_a_prefix_without_one_does_not_count():
    client = _Client(
        existing=[
            ("backups/old/manifest.json", NOW - timedelta(days=3)),
            ("backups/new/manifest.json", NOW - timedelta(hours=2)),
            ("backups/new/appdata.dump", NOW - timedelta(hours=2)),
            ("backups/partial/appdata.dump", NOW - timedelta(minutes=5)),  # a run that died before its manifest
        ]
    )

    assert backup_upload.latest(client=client) == ("backups/new", NOW - timedelta(hours=2))


def test_no_backup_at_all_is_none():
    assert backup_upload.latest(client=_Client()) is None
    assert backup_upload.latest(client=_Client(existing=[("backups/x/appdata.dump", NOW)])) is None


@pytest.mark.parametrize(("age_hours", "limit", "exit_code"), [(2, 30, 0), (31, 30, 1), (31, 48, 0)])
def test_latest_fails_when_the_newest_backup_is_older_than_the_limit(monkeypatch, capsys, age_hours, limit, exit_code):
    client = _Client(existing=[("backups/b/manifest.json", datetime.now(UTC) - timedelta(hours=age_hours))])
    monkeypatch.setattr(backup_upload.object_store, "get_client", lambda: client)
    monkeypatch.setattr(backup_upload, "wire", lambda: None)

    assert backup_upload.main(["--latest", "--max-age-hours", str(limit)]) == exit_code
    assert "backups/b" in capsys.readouterr().out


def test_latest_with_no_backup_is_a_failure_and_says_so(monkeypatch, capsys):
    monkeypatch.setattr(backup_upload.object_store, "get_client", lambda: _Client())
    monkeypatch.setattr(backup_upload, "wire", lambda: None)

    assert backup_upload.main(["--latest"]) == 1
    assert "no complete backup" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--latest", "somedir"], ["somedir"]])
def test_the_arguments_are_checked(monkeypatch, argv):
    monkeypatch.setattr(backup_upload, "wire", lambda: None)

    with pytest.raises(SystemExit) as caught:
        backup_upload.main(argv)

    assert caught.value.code == 2


def test_a_failed_upload_through_the_command_line_exits_1_and_says_why(monkeypatch, directory, capsys):
    monkeypatch.setattr(backup_upload.object_store, "get_client", lambda: _Client(fail_on="appdata.dump"))
    monkeypatch.setattr(backup_upload, "wire", lambda: None)

    assert backup_upload.main([str(directory), "--prefix", "backups/x"]) == 1
    assert "backup upload failed: the object store went away" in capsys.readouterr().err


def test_the_manifest_body_is_what_a_restore_can_read_back(directory):
    client = _Client()
    backup_upload.upload(directory, "backups/x", client=client)

    manifest = json.load(io.BytesIO(client.bodies["backups/x/manifest.json"]))

    assert set(manifest) == {"created_at", "files"}


# --- download ---------------------------------------------------------------------------------------------------------


def _stored(files: dict[str, bytes], prefix="backups/x", *, manifest_override=None) -> _Client:
    client = _Client()
    client.stored = {f"{prefix}/{name}": data for name, data in files.items()}
    manifest = manifest_override or {
        "created_at": NOW.isoformat(),
        "files": {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in files.items()},
    }
    client.stored[f"{prefix}/manifest.json"] = json.dumps(manifest).encode()
    return client


def test_a_backup_is_downloaded_and_every_file_matches_its_manifest(tmp_path):
    client = _stored({"appdata.dump": b"A" * 10, "qdrant-docs.snapshot": b"Q" * 25})

    manifest = backup_upload.download("backups/x", tmp_path, client=client)

    assert (tmp_path / "appdata.dump").read_bytes() == b"A" * 10 and (tmp_path / "qdrant-docs.snapshot").read_bytes() == b"Q" * 25
    assert set(manifest["files"]) == {"appdata.dump", "qdrant-docs.snapshot"}


def test_a_backup_with_no_manifest_is_refused_because_it_may_be_a_half_finished_upload(tmp_path):
    client = _Client()
    client.stored = {"backups/x/appdata.dump": b"A"}

    with pytest.raises(FileNotFoundError, match="no complete backup"):
        backup_upload.download("backups/x", tmp_path, client=client)

    assert list(tmp_path.iterdir()) == [], "nothing was downloaded from a backup that is not complete"


def test_a_file_that_is_not_what_the_manifest_says_fails_the_download_and_is_not_left_behind(tmp_path):
    good = {"appdata.dump": b"A" * 10}
    client = _stored(good)
    client.stored["backups/x/appdata.dump"] = b"B" * 10  # damaged or tampered with in the bucket: same size, other content

    with pytest.raises(ValueError, match=r"appdata\.dump does not match the manifest"):
        backup_upload.download("backups/x", tmp_path, client=client)

    assert not (tmp_path / "appdata.dump").exists(), "a file that failed the check must not look usable"


def test_a_truncated_file_is_caught_by_its_size(tmp_path):
    client = _stored({"appdata.dump": b"A" * 10})
    client.stored["backups/x/appdata.dump"] = b"A" * 4

    with pytest.raises(ValueError):
        backup_upload.download("backups/x", tmp_path, client=client)


@pytest.mark.parametrize("prefix", ["", "x", "uploads/x", "backups/x/"])
def test_a_download_prefix_outside_backups_is_refused(tmp_path, prefix):
    with pytest.raises(ValueError):
        backup_upload.download(prefix, tmp_path, client=_Client())


def test_what_was_uploaded_downloads_back_identical(tmp_path, directory):
    client = _Client()
    uploaded: dict[str, bytes] = {}
    client.fput_object = lambda bucket, key, path, **kw: uploaded.__setitem__(key, Path(path).read_bytes())
    client.put_object = lambda bucket, key, data, length, content_type=None, **kw: uploaded.__setitem__(key, data.read())
    backup_upload.upload(directory, "backups/round", client=client)
    client.stored = uploaded

    backup_upload.download("backups/round", tmp_path / "back", client=client)

    assert (tmp_path / "back" / "appdata.dump").read_bytes() == b"A" * 10
    assert (tmp_path / "back" / "qdrant-docs.snapshot").read_bytes() == b"Q" * 25


def test_the_download_command_reports_success_and_failure_by_its_exit_code(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(backup_upload, "wire", lambda: None)
    monkeypatch.setattr(backup_upload.object_store, "get_client", lambda: _stored({"appdata.dump": b"A" * 10}))

    assert backup_upload.main(["--download", "backups/x", "--to", str(tmp_path / "ok")]) == 0
    assert "every file matches its manifest" in capsys.readouterr().out

    broken = _stored({"appdata.dump": b"A" * 10})
    broken.stored["backups/x/appdata.dump"] = b"Z" * 10
    monkeypatch.setattr(backup_upload.object_store, "get_client", lambda: broken)
    assert backup_upload.main(["--download", "backups/x", "--to", str(tmp_path / "bad")]) == 1
    assert "does not match the manifest" in capsys.readouterr().err
