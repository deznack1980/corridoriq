"""Off-site backup replication to R2.

Closeout risk #1 was that local backups shared a volume with the database.
These tests use an in-memory stand-in for the S3 API so they run without
credentials or network, and focus on the properties that decide whether the
off-site copy is trustworthy:

  * a corrupted or truncated upload is detected, not reported as success
  * an unverified local backup is never replicated
  * replication failure never breaks the backup that already succeeded
  * a replica can actually be restored and opened as a database
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.config import settings
from pipeline.db import backup, replication


# ---------------------------------------------------------------------------
# A minimal stand-in for the S3 surface this module uses.
# ---------------------------------------------------------------------------

class FakeS3:
    def __init__(self):
        self.objects: dict[str, dict] = {}
        self.uploads = 0
        self.fail_upload = None
        self.corrupt_on_download = False

    def upload_file(self, filename, bucket, key, ExtraArgs=None, Config=None):
        if self.fail_upload:
            raise self.fail_upload
        self.uploads += 1
        data = Path(filename).read_bytes()
        self.objects[key] = {"body": data, "size": len(data),
                             "meta": dict((ExtraArgs or {}).get("Metadata", {}))}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(f"404 {Key}")
        obj = self.objects[Key]
        return {"ContentLength": obj["size"], "Metadata": obj["meta"]}

    def list_objects_v2(self, Bucket, Prefix="", ContinuationToken=None):
        contents = [{"Key": k, "Size": v["size"]}
                    for k, v in sorted(self.objects.items())
                    if k.startswith(Prefix)]
        return {"Contents": contents, "IsTruncated": False}

    def download_file(self, bucket, key, filename, Config=None):
        data = self.objects[key]["body"]
        if self.corrupt_on_download:
            data = data[:-64] + b"\x00" * 64
        Path(filename).write_bytes(data)

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


@pytest.fixture()
def r2(monkeypatch, tmp_path):
    """Configured replication pointed at a fake bucket and a temp backup dir."""
    monkeypatch.setattr(settings, "R2_REPLICATION_ENABLED", True)
    monkeypatch.setattr(settings, "R2_BUCKET", "corridoriq-backups")
    monkeypatch.setattr(settings, "R2_PREFIX", "db-backups")
    monkeypatch.setattr(settings, "R2_ACCOUNT_ID", "acct")
    monkeypatch.setattr(settings, "R2_ACCESS_KEY_ID", "key")
    monkeypatch.setattr(settings, "R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setattr(settings, "R2_KEEP_DAILY", 30)
    monkeypatch.setattr(settings, "R2_KEEP_WEEKLY", 26)

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    monkeypatch.setattr(backup, "DB_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(replication, "list_backups",
                        lambda *a, **k: backup.list_backups(backup_dir))

    client = FakeS3()
    monkeypatch.setattr(replication, "build_client", lambda: client)
    monkeypatch.setattr(replication, "_transfer_config", lambda: None)
    return client, backup_dir


def _make_backup(backup_dir: Path, stamp: str = "20260915T120000Z") -> Path:
    """A real SQLite file so integrity checks are meaningful."""
    path = backup_dir / f"corridoriq_{stamp}.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE permits (id INTEGER PRIMARY KEY, n TEXT)")
    conn.executemany("INSERT INTO permits (n) VALUES (?)",
                     [(f"P-{i}",) for i in range(50)])
    conn.commit()
    conn.close()
    return path


# ==========================================================================
# Configuration
# ==========================================================================

def test_missing_credentials_disable_replication(monkeypatch):
    monkeypatch.setattr(settings, "R2_REPLICATION_ENABLED", False)
    assert replication.configured() is False
    assert replication.replicate_safe() is None


def test_unconfigured_client_raises_a_useful_error(monkeypatch):
    monkeypatch.setattr(settings, "R2_REPLICATION_ENABLED", False)
    with pytest.raises(replication.ReplicationNotConfigured) as exc:
        replication.build_client()
    assert "CORRIDORIQ_R2_ACCOUNT_ID" in str(exc.value)


def test_key_is_namespaced_by_prefix(r2):
    assert replication.remote_key("corridoriq_x.db") == "db-backups/corridoriq_x.db"


# ==========================================================================
# Upload and verification
# ==========================================================================

def test_upload_stores_checksum_rows_and_host(r2):
    client, backup_dir = r2
    path = _make_backup(backup_dir)

    result = replication.upload(path)

    assert result["sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert result["rows"] == 50
    stored = client.objects["db-backups/corridoriq_20260915T120000Z.db"]["meta"]
    assert stored["sha256"] == result["sha256"]
    assert stored["rows"] == "50"
    assert stored["source-host"]


def test_size_mismatch_after_upload_is_an_error(r2, monkeypatch):
    """A truncated upload must not be reported as a success."""
    client, backup_dir = r2
    path = _make_backup(backup_dir)

    real_head = client.head_object
    monkeypatch.setattr(client, "head_object",
                        lambda **kw: {**real_head(**kw), "ContentLength": 123})

    with pytest.raises(RuntimeError, match="size mismatch"):
        replication.upload(path)


def test_checksum_mismatch_after_upload_is_an_error(r2, monkeypatch):
    client, backup_dir = r2
    path = _make_backup(backup_dir)

    real_head = client.head_object
    monkeypatch.setattr(client, "head_object",
                        lambda **kw: {**real_head(**kw), "Metadata": {"sha256": "nope"}})

    with pytest.raises(RuntimeError, match="checksum metadata mismatch"):
        replication.upload(path)


def test_a_corrupt_local_backup_is_never_replicated(r2):
    """Replicating a broken backup would turn one bad copy into two."""
    client, backup_dir = r2
    path = backup_dir / "corridoriq_20260915T120000Z.db"
    path.write_bytes(b"this is not a database" * 100)

    with pytest.raises(RuntimeError, match="unverified backup"):
        replication.upload(path)
    assert client.uploads == 0


# ==========================================================================
# replicate_latest
# ==========================================================================

def test_replicates_the_newest_backup(r2):
    client, backup_dir = r2
    _make_backup(backup_dir, "20260914T120000Z")
    _make_backup(backup_dir, "20260915T120000Z")

    result = replication.replicate_latest()

    assert result["key"].endswith("20260915T120000Z.db")
    assert result["skipped"] is False


def test_an_already_replicated_backup_is_not_re_uploaded(r2):
    client, backup_dir = r2
    _make_backup(backup_dir)

    replication.replicate_latest()
    second = replication.replicate_latest()

    assert second["skipped"] is True
    assert client.uploads == 1


def test_no_local_backups_is_not_an_error(r2):
    assert replication.replicate_latest() is None


# ==========================================================================
# Failure isolation
# ==========================================================================

def test_replication_failure_does_not_break_the_backup(r2, capsys):
    client, backup_dir = r2
    _make_backup(backup_dir)
    client.fail_upload = ConnectionError("network unreachable")

    assert replication.replicate_safe() is None
    assert "local backup is unaffected" in capsys.readouterr().out


def test_backup_paths_resolve_at_call_time(monkeypatch, tmp_path):
    """Regression: backup paths were bound as default arguments at import, so
    overriding DB_BACKUP_DIR was silently ignored and the module operated on
    production regardless. Late binding is what makes redirection possible --
    and what stops a test run from pruning real backups."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(backup, "DB_BACKUP_DIR", elsewhere)

    source = tmp_path / "src.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup, "DB_PATH", source)

    created = backup.create_backup()
    assert created.parent == elsewhere
    assert backup.list_backups()[0][0].parent == elsewhere


def test_backup_job_completes_when_replication_fails(r2, monkeypatch, tmp_path, capsys):
    """The end-to-end guarantee: the local backup survives a dead network."""
    client, backup_dir = r2
    client.fail_upload = ConnectionError("network unreachable")

    source = tmp_path / "prod.db"
    conn = sqlite3.connect(source)
    conn.execute("CREATE TABLE t (id INTEGER)")
    conn.commit()
    conn.close()

    monkeypatch.setattr(backup, "DB_PATH", source)
    monkeypatch.setattr("sys.argv", ["backup"])
    assert backup.main() == 0
    assert len(backup.list_backups(backup_dir)) == 1


# ==========================================================================
# Deep verification and restore
# ==========================================================================

def test_verify_deep_passes_for_an_intact_replica(r2):
    client, backup_dir = r2
    _make_backup(backup_dir)
    replication.replicate_latest()

    result = replication.verify_deep()

    assert result["checksum_match"] is True
    assert result["integrity_ok"] is True
    assert result["rows"] == 50


def test_verify_deep_catches_bytes_that_rotted_in_transit(r2):
    """Metadata can look right while the object itself is wrong. Only reading
    it back proves otherwise."""
    client, backup_dir = r2
    _make_backup(backup_dir)
    replication.replicate_latest()
    client.corrupt_on_download = True

    result = replication.verify_deep()
    assert result["checksum_match"] is False


def test_a_replica_can_be_restored_and_queried(r2, tmp_path):
    client, backup_dir = r2
    _make_backup(backup_dir)
    replication.replicate_latest()

    dest = tmp_path / "restored.db"
    replication.download("corridoriq_20260915T120000Z.db", dest)

    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 50
    conn.close()


# ==========================================================================
# Remote retention and status
# ==========================================================================

def _entry(stamp: str) -> dict:
    return {"key": f"db-backups/corridoriq_{stamp}.db",
            "name": f"corridoriq_{stamp}.db",
            "moment": datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(
                tzinfo=timezone.utc),
            "bytes": 1}


def test_remote_retention_keeps_newest_per_day(r2):
    entries = [_entry("20260915T180000Z"), _entry("20260915T060000Z"),
               _entry("20260914T060000Z")]
    expired = replication.select_expired_remote(entries, keep_daily=30, keep_weekly=26)
    assert [e["name"] for e in expired] == ["corridoriq_20260915T060000Z.db"]


def test_remote_retention_is_deeper_than_local(r2):
    """Off-site is the copy that matters when the local disk is gone."""
    assert settings.R2_KEEP_DAILY >= settings.DB_BACKUP_KEEP_DAILY
    assert settings.R2_KEEP_WEEKLY >= settings.DB_BACKUP_KEEP_WEEKLY


def test_prune_removes_expired_replicas(r2):
    client, backup_dir = r2
    _make_backup(backup_dir, "20260915T060000Z")
    replication.replicate_latest()
    _make_backup(backup_dir, "20260915T180000Z")
    replication.replicate_latest()

    removed = replication.prune_remote(keep_daily=1, keep_weekly=1)
    assert removed == ["corridoriq_20260915T060000Z.db"]
    assert len(replication.list_remote()) == 1


def test_status_reports_backups_missing_off_site(r2):
    client, backup_dir = r2
    _make_backup(backup_dir, "20260914T120000Z")
    _make_backup(backup_dir, "20260915T120000Z")
    replication.replicate_latest()  # newest only

    info = replication.status()
    assert info["unreplicated"] == ["corridoriq_20260914T120000Z.db"]


def test_status_is_clean_when_everything_is_replicated(r2):
    client, backup_dir = r2
    _make_backup(backup_dir)
    replication.replicate_latest()

    assert replication.status()["unreplicated"] == []
