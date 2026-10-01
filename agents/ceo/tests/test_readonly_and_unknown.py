"""Read-only database behavior and UNKNOWN metrics."""

from __future__ import annotations

import sqlite3

import pytest

from agents.ceo.operating_state.collector import collect_database, connect_readonly
from agents.ceo.operating_state.queries import SELECTS
from agents.ceo.operating_state.snapshot import build_snapshot
from agents.ceo.paths import FIXTURE_DIR
from agents.framework.provenance import UNKNOWN


def test_selects_are_read_only():
    for name, sql in SELECTS.items():
        assert sql.lstrip().upper().startswith("SELECT"), name


def test_readonly_connection_rejects_writes(tmp_path):
    db = tmp_path / "sample.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE projects (id INTEGER, opportunity_score REAL)")
    conn.execute("INSERT INTO projects VALUES (1, 42)")
    conn.commit()
    conn.close()

    ro = connect_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("UPDATE projects SET opportunity_score = 1")
    ro.close()

    check = sqlite3.connect(db)
    score = check.execute("SELECT opportunity_score FROM projects").fetchone()[0]
    check.close()
    assert score == 42


def test_collector_does_not_change_rows(tmp_path):
    db = tmp_path / "sample.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE projects (id INTEGER, opportunity_score REAL)")
    conn.execute("INSERT INTO projects VALUES (1, 10)")
    conn.commit()
    conn.close()

    metrics = collect_database(db)
    assert metrics["project_count"]["status"] == "KNOWN"
    assert metrics["project_count"]["value"] == 1
    assert metrics["permit_count"]["status"] == UNKNOWN

    check = sqlite3.connect(db)
    score = check.execute("SELECT opportunity_score FROM projects").fetchone()[0]
    check.close()
    assert score == 10


def test_missing_database_is_unknown(tmp_path):
    metrics = collect_database(tmp_path / "absent.db")
    assert metrics["project_count"]["status"] == UNKNOWN
    assert metrics["project_count"]["value"] is None


def test_snapshot_labels_unknown_without_inventing_counts(tmp_path):
    snapshot = build_snapshot(reports_dir=tmp_path, connect_db=False)
    assert snapshot["commercial"]["revenue"]["status"] == UNKNOWN
    assert snapshot["commercial"]["pricing"]["status"] == UNKNOWN
    assert snapshot["data_health"]["permit_count"]["status"] == UNKNOWN
    assert snapshot["sales"]["actionable_public_contact_pct"]["status"] == UNKNOWN
    assert "commercial.revenue" in snapshot["unknowns"]


def test_fixture_snapshot_keeps_permit_count_unknown():
    snapshot = build_snapshot(reports_dir=FIXTURE_DIR, connect_db=False)
    assert snapshot["data_health"]["permit_count"]["status"] == UNKNOWN
    assert snapshot["data_health"]["project_count"]["value"] == 116336
    assert snapshot["sales"]["actionable_public_contact_pct"]["value"] == 96.0
    assert snapshot["sales"]["recorded_field_outcomes"]["value"] == 0
    assert len(snapshot["sales"]["learning_trial_accounts"]["value"]) == 4
