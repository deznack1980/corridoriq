"""Data platform Phase 0 + Phase 1 tests.

Phase 0 — operational safety: relocation verification, verified backups,
retention pruning.
Phase 1 — RAW append-only capture: hashing, versioning, idempotency, the
dual-write into both ingestion paths, and the baseline backfill.

The load-bearing guarantees asserted here:
  * an unchanged re-fetch writes NOTHING (kills the no-op update churn)
  * a value that changes A -> B -> A records three versions
  * RAW never mutates or blocks the existing permits behaviour
  * a RAW failure degrades to a no-op rather than breaking ingestion
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.config import settings
from pipeline.config.settings import SCHEMA_PATH
from pipeline.connectors.base import (
    PERMIT_FIELDS,
    BaseConnector,
    ConnectorNotConfiguredError,
    ConnectorResult,
)
from pipeline.db import backup as db_backup
from pipeline.ingestion import raw_capture
from pipeline.ingestion.raw_backfill import backfill_permits
from pipeline.ingestion.run_ingestion import ingest_jurisdiction, run_ingestion


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "raw.db")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for slug, name in (("phoenix_az", "Phoenix"), ("mesa_az", "Mesa")):
        c.execute(
            "INSERT INTO jurisdictions (slug, name, state, status, connector_type, "
            "endpoint_url) VALUES (?,?,'AZ','connected','arcgis_hub','https://example.test/x')",
            (slug, name),
        )
    c.commit()
    return c


def _permit(**kw) -> dict:
    d = {k: None for k in PERMIT_FIELDS}
    d["jurisdiction"] = "phoenix_az"
    d["raw_source_json"] = '{"a": 1}'
    d.update(kw)
    return d


class FakeConnector:
    def __init__(self, slug, records):
        self.slug = slug
        self.records = records

    def run(self, since=None):
        out = []
        for r in self.records:
            m = dict(r)
            m["jurisdiction"] = self.slug
            m.setdefault("raw_source_json", "{}")
            out.append(m)
        return ConnectorResult(jurisdiction_slug=self.slug, records=out)


def _install(monkeypatch, mapping):
    def _build(slug, connector_type):
        if slug in mapping:
            return mapping[slug]
        raise ConnectorNotConfiguredError(slug)
    monkeypatch.setattr("pipeline.ingestion.run_ingestion.build_connector", _build)


def _open_batch(conn):
    return raw_capture.open_batch(conn, "phoenix_az", "permit")


# ==========================================================================
# Hashing
# ==========================================================================

def test_hash_ignores_key_order_and_whitespace():
    a = raw_capture.payload_hash('{"b": 2, "a": 1}')
    b = raw_capture.payload_hash('{"a":1,"b":2}')
    c = raw_capture.payload_hash({"a": 1, "b": 2})
    assert a == b == c


def test_hash_changes_with_value():
    assert raw_capture.payload_hash('{"a": 1}') != raw_capture.payload_hash('{"a": 2}')


def test_non_json_payload_still_hashes():
    """An opaque payload must be captured, not dropped."""
    assert raw_capture.payload_hash("not json at all")
    assert raw_capture.payload_hash("not json at all") == raw_capture.payload_hash(
        "not json at all"
    )


def test_esri_objectid_does_not_count_as_a_change():
    """Chandler's feed returns two rows per permit that are identical except
    for OBJECTID, an Esri row id. That must not look like a change."""
    a = '{"BLD_F_B1_ALT_ID": "BLD18-2830", "OBJECTID": 1808, "BLD_L_JOB_VALUE": "$100"}'
    b = '{"BLD_F_B1_ALT_ID": "BLD18-2830", "OBJECTID": 1551, "BLD_L_JOB_VALUE": "$100"}'
    assert raw_capture.payload_hash(a) == raw_capture.payload_hash(b)


def test_real_field_change_is_still_detected_alongside_objectid():
    a = '{"OBJECTID": 1, "BLD_L_JOB_VALUE": "$100"}'
    b = '{"OBJECTID": 2, "BLD_L_JOB_VALUE": "$250"}'
    assert raw_capture.payload_hash(a) != raw_capture.payload_hash(b)


def test_socrata_system_fields_are_excluded():
    a = '{"permit_number": "X", ":id": "abc", ":updated_at": "2026-01-01"}'
    b = '{"permit_number": "X", ":id": "zzz", ":updated_at": "2026-09-09"}'
    assert raw_capture.payload_hash(a) == raw_capture.payload_hash(b)


def test_full_payload_is_stored_even_when_excluded_from_the_hash(conn):
    """Exclusion affects change detection only - nothing is thrown away."""
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "chandler_az", "BLD18-2830",
                        '{"OBJECTID": 1808, "v": 1}')
    stored = raw_capture.current_version(conn, "chandler_az", "BLD18-2830")["payload_json"]
    assert "OBJECTID" in stored and "1808" in stored


def test_hash_rule_change_does_not_manufacture_versions(conn, monkeypatch):
    """Changing the hashing rule must not make the whole corpus look changed."""
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"OBJECTID": 1, "v": 1}')
    # Simulate a row written under an older rule (hash over the full payload).
    conn.execute(
        "UPDATE raw_record SET payload_hash = ?, payload_hash_version = 1 "
        "WHERE source_record_id = 'P-1'",
        ("hash_from_an_older_rule",),
    )
    conn.commit()

    assert raw_capture.capture(conn, b, "phoenix_az", "P-1",
                               '{"OBJECTID": 1, "v": 1}') == raw_capture.UNCHANGED
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 1
    row = raw_capture.current_version(conn, "phoenix_az", "P-1")
    assert row["payload_hash_version"] == raw_capture.HASH_VERSION
    assert row["payload_hash"] != "hash_from_an_older_rule"


def test_hash_rule_change_still_detects_a_genuine_change(conn):
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 1}')
    conn.execute("UPDATE raw_record SET payload_hash_version = 1")
    conn.commit()

    assert raw_capture.capture(conn, b, "phoenix_az", "P-1",
                               '{"v": 2}') == raw_capture.CHANGED
    assert [r["version_number"] for r in
            raw_capture.version_history(conn, "phoenix_az", "P-1")] == [1, 2]


def test_source_updated_at_read_from_socrata_only():
    assert raw_capture.extract_source_updated_at(
        '{"permit_number": "X", ":updated_at": "2026-09-09T00:00:00"}'
    ) == "2026-09-09T00:00:00"
    # Esri editor-tracking fields are not trusted until verified per source.
    assert raw_capture.extract_source_updated_at('{"EditDate": 1757894400000}') is None
    assert raw_capture.extract_source_updated_at("not json") is None


# ==========================================================================
# Capture semantics
# ==========================================================================

def test_first_capture_is_new(conn):
    b = _open_batch(conn)
    assert raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 1}') == raw_capture.NEW
    row = raw_capture.current_version(conn, "phoenix_az", "P-1")
    assert row["version_number"] == 1
    assert row["is_current"] == 1


def test_identical_repeat_writes_nothing(conn):
    """The churn fix: re-fetching an unchanged record must not write a row."""
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 1}')
    before = conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0]

    for _ in range(5):
        assert raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 1}') == (
            raw_capture.UNCHANGED
        )

    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == before


def test_change_creates_second_version_and_supersedes_first(conn):
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"status": "Applied"}')
    assert raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"status": "Issued"}') == (
        raw_capture.CHANGED
    )

    history = raw_capture.version_history(conn, "phoenix_az", "P-1")
    assert [r["version_number"] for r in history] == [1, 2]
    assert [r["is_current"] for r in history] == [0, 1]
    # The superseded payload is still readable - this is the whole point.
    assert "Applied" in history[0]["payload_json"]
    assert "Issued" in history[1]["payload_json"]


def test_value_reverting_records_a_third_version(conn):
    """A -> B -> A is a real event. Hash-keyed uniqueness would have lost it."""
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"s": "A"}')
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"s": "B"}')
    assert raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"s": "A"}') == (
        raw_capture.CHANGED
    )

    history = raw_capture.version_history(conn, "phoenix_az", "P-1")
    assert [r["version_number"] for r in history] == [1, 2, 3]
    assert raw_capture.current_version(conn, "phoenix_az", "P-1")["version_number"] == 3


def test_only_one_current_version_per_record(conn):
    b = _open_batch(conn)
    for value in range(4):
        raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": %d}' % value)
    n = conn.execute(
        "SELECT COUNT(*) FROM raw_record WHERE source_system='phoenix_az' "
        "AND source_record_id='P-1' AND is_current=1"
    ).fetchone()[0]
    assert n == 1


def test_database_rejects_a_second_current_version(conn):
    """The single-current rule is enforced by the schema, not by convention."""
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 1}')
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO raw_record (batch_id, source_system, source_entity_type, "
            "source_record_id, payload_json, payload_hash, fetched_at, "
            "version_number, is_current) VALUES (?,?,?,?,?,?,?,?,1)",
            (b, "phoenix_az", "permit", "P-1", "{}", "deadbeef", "2026-01-01", 99),
        )


def test_records_are_scoped_per_source_system(conn):
    """The same permit number in two cities is two different records."""
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 1}')
    assert raw_capture.capture(conn, b, "mesa_az", "P-1", '{"v": 1}') == raw_capture.NEW
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 2


def test_missing_source_record_id_is_a_noop(conn):
    """Inventing a key would corrupt the lineage this layer exists to provide."""
    b = _open_batch(conn)
    assert raw_capture.capture(conn, b, "phoenix_az", "", '{"v": 1}') == raw_capture.UNCHANGED
    assert raw_capture.capture(conn, b, "phoenix_az", None, '{"v": 1}') == raw_capture.UNCHANGED
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 0


def test_capture_permit_uses_jurisdiction_and_permit_number(conn):
    b = _open_batch(conn)
    mapped = _permit(permit_number="ABC-9", raw_source_json='{"PER_NUM": "ABC-9"}')
    assert raw_capture.capture_permit(conn, b, mapped) == raw_capture.NEW
    row = raw_capture.current_version(conn, "phoenix_az", "ABC-9")
    assert row["source_entity_type"] == settings.RAW_ENTITY_PERMIT


def test_fetched_at_can_be_backdated(conn):
    """The backfill records when we first saw a permit, not when it ran."""
    b = _open_batch(conn)
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 1}',
                        fetched_at="2024-07-15T00:00:00+00:00")
    assert raw_capture.current_version(conn, "phoenix_az", "P-1")["fetched_at"].startswith(
        "2024-07-15"
    )


# ==========================================================================
# Batch lifecycle
# ==========================================================================

def test_batch_records_counts_and_closes(conn):
    b = raw_capture.open_batch(conn, "phoenix_az", "permit", connector_type="arcgis_hub")
    assert conn.execute(
        "SELECT status FROM raw_ingest_batch WHERE batch_id=?", (b,)
    ).fetchone()[0] == "running"

    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 1}')
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 2}')
    raw_capture.capture(conn, b, "phoenix_az", "P-1", '{"v": 2}')
    raw_capture.close_batch(conn, b, fetched=3, new=1, changed=1, unchanged=1)

    row = conn.execute("SELECT * FROM raw_ingest_batch WHERE batch_id=?", (b,)).fetchone()
    assert row["status"] == "succeeded"
    assert (row["records_new"], row["records_changed"], row["records_unchanged"]) == (1, 1, 1)
    assert row["completed_at"] is not None


# ==========================================================================
# Dual-write into ingestion (both paths)
# ==========================================================================

def test_full_ingest_path_captures_raw(conn, monkeypatch):
    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", [
            _permit(permit_number="A-1", status="Issued", raw_source_json='{"n": "A-1"}'),
            _permit(permit_number="A-2", status="Applied", raw_source_json='{"n": "A-2"}'),
        ]),
        "mesa_az": FakeConnector("mesa_az", []),
    })
    run_ingestion(conn)

    assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 2
    batch = conn.execute(
        "SELECT * FROM raw_ingest_batch WHERE source_system='phoenix_az'"
    ).fetchone()
    assert batch["records_new"] == 2
    assert batch["connector_type"] == "arcgis_hub"
    assert batch["source_url"] == "https://example.test/x"
    assert batch["status"] == "succeeded"


def test_morning_refresh_path_captures_raw(conn, monkeypatch):
    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", [
            _permit(permit_number="A-1", raw_source_json='{"n": "A-1"}'),
        ]),
    })
    stats = ingest_jurisdiction(conn, "phoenix_az", "arcgis_hub", None)
    assert stats["raw_new"] == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 1


def test_repeat_ingest_captures_no_new_versions(conn, monkeypatch):
    """Two identical runs: permits behave as before, RAW stays at one version."""
    records = [_permit(permit_number="A-1", status="Issued", raw_source_json='{"n": 1}')]
    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", records),
        "mesa_az": FakeConnector("mesa_az", []),
    })
    run_ingestion(conn)
    run_ingestion(conn)

    assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 1
    second = conn.execute(
        "SELECT * FROM raw_ingest_batch WHERE source_system='phoenix_az' "
        "ORDER BY batch_id DESC LIMIT 1"
    ).fetchone()
    assert (second["records_new"], second["records_unchanged"]) == (0, 1)


def test_changed_source_record_creates_a_new_raw_version(conn, monkeypatch):
    """The end-to-end guarantee: a status change is preserved even though the
    permits row itself is overwritten in place."""
    connector = FakeConnector("phoenix_az", [
        _permit(permit_number="A-1", status="Applied", raw_source_json='{"s": "Applied"}'),
    ])
    _install(monkeypatch, {"phoenix_az": connector, "mesa_az": FakeConnector("mesa_az", [])})
    run_ingestion(conn)

    connector.records = [
        _permit(permit_number="A-1", status="Issued", raw_source_json='{"s": "Issued"}')
    ]
    run_ingestion(conn)

    # permits: overwritten, only the new value survives (unchanged behaviour)
    assert conn.execute("SELECT status FROM permits").fetchone()[0] == "Issued"
    # RAW: both observations retained
    history = raw_capture.version_history(conn, "phoenix_az", "A-1")
    assert len(history) == 2
    assert "Applied" in history[0]["payload_json"]
    assert "Issued" in history[1]["payload_json"]


def test_raw_failure_does_not_break_ingestion(conn, monkeypatch):
    """RAW is observability, not a dependency. Losing permits is worse than
    losing raw versions."""
    def boom(*a, **kw):
        raise sqlite3.OperationalError("raw layer exploded")
    monkeypatch.setattr(raw_capture, "capture_permit", boom)
    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", [_permit(permit_number="A-1")]),
        "mesa_az": FakeConnector("mesa_az", []),
    })

    run_ingestion(conn)
    assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 1


def test_capture_can_be_disabled_by_setting(conn, monkeypatch):
    monkeypatch.setattr(settings, "RAW_CAPTURE_ENABLED", False)
    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", [_permit(permit_number="A-1")]),
        "mesa_az": FakeConnector("mesa_az", []),
    })
    run_ingestion(conn)

    assert conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 0


# ==========================================================================
# Source duplicate handling (Chandler DSActiveProjects defect)
# ==========================================================================

class _RawConnector(BaseConnector):
    """Exercises the real BaseConnector.run(), including dedupe."""

    def __init__(self, slug, raws, field_map=None):
        super().__init__(slug)
        self.raws = raws
        self.field_map = field_map or {"permit_number": "ID", "status": "STATUS"}

    def fetch_raw(self, since=None):
        return iter(self.raws)

    def map_record(self, raw):
        mapped = self._empty_permit_dict()
        for target, source in self.field_map.items():
            mapped[target] = raw.get(source)
        return mapped


def test_exact_duplicate_rows_are_dropped():
    """Chandler publishes every permit twice, differing only in OBJECTID."""
    connector = _RawConnector("chandler_az", [
        {"ID": "BLD20-0676", "STATUS": "Under Construction", "OBJECTID": 1499},
        {"ID": "BLD20-0676", "STATUS": "Under Construction", "OBJECTID": 1756},
    ])
    result = connector.run()

    assert result.source_row_count == 2
    assert result.duplicates_dropped == 1
    assert result.fetched_count == 1


def test_conflicting_duplicates_are_both_kept():
    """Same permit number, different data, is a real conflict - keep it visible
    rather than silently resolving it inside the connector."""
    connector = _RawConnector("chandler_az", [
        {"ID": "BLD20-0676", "STATUS": "Under Construction", "OBJECTID": 1},
        {"ID": "BLD20-0676", "STATUS": "Completed", "OBJECTID": 2},
    ])
    result = connector.run()

    assert result.duplicates_dropped == 0
    assert result.fetched_count == 2


def test_distinct_permits_are_never_deduplicated():
    connector = _RawConnector("chandler_az", [
        {"ID": "A-1", "STATUS": "x", "OBJECTID": 1},
        {"ID": "A-2", "STATUS": "x", "OBJECTID": 2},
    ])
    result = connector.run()
    assert result.duplicates_dropped == 0
    assert result.fetched_count == 2


def test_duplicate_rows_do_not_produce_duplicate_raw_versions(conn, monkeypatch):
    """End to end: the source defect must not create phantom RAW history."""
    connector = _RawConnector("chandler_az", [
        {"ID": "BLD20-0676", "STATUS": "Under Construction", "OBJECTID": 1499},
        {"ID": "BLD20-0676", "STATUS": "Under Construction", "OBJECTID": 1756},
    ])
    conn.execute("INSERT INTO jurisdictions (slug, name, state, status, connector_type) "
                 "VALUES ('chandler_az','Chandler','AZ','connected','arcgis_hub')")
    conn.commit()
    _install(monkeypatch, {"chandler_az": connector,
                           "phoenix_az": FakeConnector("phoenix_az", []),
                           "mesa_az": FakeConnector("mesa_az", [])})
    run_ingestion(conn)

    assert conn.execute(
        "SELECT COUNT(*) FROM raw_record WHERE source_system='chandler_az'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM permits WHERE jurisdiction='chandler_az'"
    ).fetchone()[0] == 1


def test_a_bad_record_does_not_stop_the_batch():
    class Exploding(_RawConnector):
        def map_record(self, raw):
            if raw.get("ID") == "BOOM":
                raise ValueError("unmappable")
            return super().map_record(raw)

    result = Exploding("chandler_az", [
        {"ID": "A-1", "STATUS": "x"},
        {"ID": "BOOM"},
        {"ID": "A-2", "STATUS": "x"},
    ]).run()

    assert result.fetched_count == 2
    assert len(result.errors) == 1
    assert result.source_row_count == 3


# ==========================================================================
# Baseline backfill
# ==========================================================================

def _seed_permit(conn, number, payload='{"x": 1}', first_seen="2024-07-15T00:00:00+00:00"):
    conn.execute(
        "INSERT INTO permits (jurisdiction, permit_number, raw_source_json, "
        "first_seen_at, last_updated_at) VALUES ('phoenix_az', ?, ?, ?, ?)",
        (number, payload, first_seen, "2026-09-15T00:00:00+00:00"),
    )
    conn.commit()


def test_backfill_seeds_one_version_per_permit(conn):
    for i in range(5):
        _seed_permit(conn, f"B-{i}")
    stats = backfill_permits(conn)

    assert stats["scanned"] == 5
    assert stats["new"] == 5
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 5
    assert conn.execute(
        "SELECT COUNT(*) FROM raw_record WHERE version_number=1"
    ).fetchone()[0] == 5


def test_backfill_is_idempotent(conn):
    for i in range(3):
        _seed_permit(conn, f"B-{i}")
    backfill_permits(conn)
    second = backfill_permits(conn)

    assert second["new"] == 0
    assert second["unchanged"] == 3
    assert conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0] == 3


def test_backfill_backdates_to_first_seen(conn):
    _seed_permit(conn, "B-1", first_seen="2024-07-15T00:00:00+00:00")
    backfill_permits(conn)
    row = raw_capture.current_version(conn, "phoenix_az", "B-1")
    assert row["fetched_at"].startswith("2024-07-15")


def test_backfill_skips_permits_without_a_payload(conn):
    _seed_permit(conn, "B-1", payload="{}")
    _seed_permit(conn, "B-2", payload=None)
    _seed_permit(conn, "B-3", payload='{"real": true}')
    stats = backfill_permits(conn)

    assert stats["skipped"] == 2
    assert stats["new"] == 1


def test_backfill_batch_is_labelled_as_a_backfill(conn):
    """Backfilled rows are a snapshot, not observed history - and must say so."""
    _seed_permit(conn, "B-1")
    stats = backfill_permits(conn)
    batch = conn.execute(
        "SELECT * FROM raw_ingest_batch WHERE batch_id=?", (stats["batch_id"],)
    ).fetchone()
    assert batch["connector_type"] == "backfill"
    assert batch["source_system"] == settings.RAW_BACKFILL_SOURCE


def test_backfill_then_live_change_versions_correctly(conn, monkeypatch):
    """A backfilled record must accept a real observation as version 2."""
    _seed_permit(conn, "A-1", payload='{"s": "Applied"}')
    backfill_permits(conn)

    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", [
            _permit(permit_number="A-1", raw_source_json='{"s": "Issued"}')
        ]),
        "mesa_az": FakeConnector("mesa_az", []),
    })
    run_ingestion(conn)

    history = raw_capture.version_history(conn, "phoenix_az", "A-1")
    assert [r["version_number"] for r in history] == [1, 2]


# ==========================================================================
# Phase 0 — backups
# ==========================================================================

def _make_db(path):
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute(
        "INSERT INTO jurisdictions (slug, name, state, status) "
        "VALUES ('phoenix_az','Phoenix','AZ','connected')"
    )
    c.execute(
        "INSERT INTO permits (jurisdiction, permit_number, first_seen_at, last_updated_at) "
        "VALUES ('phoenix_az','X-1','2026-01-01','2026-01-01')"
    )
    c.commit()
    c.close()
    return path


def test_backup_is_created_and_verified(tmp_path):
    db = _make_db(tmp_path / "src.db")
    target = db_backup.create_backup(db, tmp_path / "backups")
    assert target.exists()
    ok, detail, rows = db_backup.verify(target)
    assert ok and detail == "ok" and rows >= 1


def test_backup_refuses_a_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        db_backup.create_backup(tmp_path / "nope.db", tmp_path / "backups")


def test_verify_rejects_a_corrupt_file(tmp_path):
    """A backup that cannot be restored must not report success."""
    bad = tmp_path / "corrupt.db"
    bad.write_bytes(b"this is definitely not a sqlite database")
    ok, _, _ = db_backup.verify(bad)
    assert ok is False


def test_retention_keeps_daily_and_weekly(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    base = datetime(2026, 9, 15, 3, 0, tzinfo=timezone.utc)
    for days_ago in range(0, 120):
        stamp = (base - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%SZ")
        (backup_dir / f"corridoriq_{stamp}.db").write_bytes(b"x")

    expired = db_backup.select_expired(db_backup.list_backups(backup_dir),
                                       keep_daily=14, keep_weekly=8)
    kept = 120 - len(expired)
    # 14 dailies plus up to 8 weekly buckets, the first two of which overlap
    # the daily window.
    assert 14 <= kept <= 22
    assert len(expired) > 90


def test_retention_never_touches_manual_snapshots(tmp_path):
    """pre_migration_* files are hand-made safety copies, not rotation fodder."""
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    manual = backup_dir / "pre_migration_20260915T082641Z_corridoriq.db"
    manual.write_bytes(b"x")
    base = datetime(2026, 9, 15, tzinfo=timezone.utc)
    for days_ago in range(0, 60):
        stamp = (base - timedelta(days=days_ago)).strftime("%Y%m%dT%H%M%SZ")
        (backup_dir / f"corridoriq_{stamp}.db").write_bytes(b"x")

    removed = db_backup.prune(backup_dir, keep_daily=14, keep_weekly=8)
    assert manual.exists()
    assert manual not in removed


# ==========================================================================
# Phase 0 — relocation safety
# ==========================================================================

def test_relocation_manifest_detects_row_count_drift():
    from scripts.migrate_database_location import compare

    assert compare({"permits": 10}, {"permits": 10}) == []
    problems = compare({"permits": 10}, {"permits": 9})
    assert len(problems) == 1 and "permits" in problems[0]


def test_relocation_refuses_to_overwrite_an_existing_target(tmp_path, capsys):
    from scripts.migrate_database_location import migrate

    src = _make_db(tmp_path / "src.db")
    dst = tmp_path / "dst.db"
    dst.write_bytes(b"already here")

    assert migrate(src, dst, dry_run=False, keep_original=False) == 1
    assert dst.read_bytes() == b"already here"
    assert src.exists()


def test_relocation_copies_verifies_and_retires(tmp_path, monkeypatch):
    from scripts import migrate_database_location as mig

    src = _make_db(tmp_path / "src.db")
    dst = tmp_path / "moved" / "corridoriq.db"
    monkeypatch.setattr(mig, "DB_BACKUP_DIR", tmp_path / "backups")

    assert mig.migrate(src, dst, dry_run=False, keep_original=False) == 0
    assert dst.exists()
    assert not src.exists()  # retired, not deleted
    retired = list((tmp_path / "backups").glob("pre_migration_*"))
    assert len(retired) == 1

    ok, _, rows = db_backup.verify(dst)
    assert ok and rows >= 1
    conn = sqlite3.connect(dst)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    conn.close()


def test_relocation_dry_run_changes_nothing(tmp_path):
    from scripts.migrate_database_location import migrate

    src = _make_db(tmp_path / "src.db")
    dst = tmp_path / "dst.db"
    assert migrate(src, dst, dry_run=True, keep_original=False) == 0
    assert src.exists()
    assert not dst.exists()
