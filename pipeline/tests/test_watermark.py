"""The incremental watermark must track source data, not wall-clock run time.

Regression cover for the defect telemetry exposed on 2026-09-15: the watermark
was `last_synced_at`, while connectors filter on the source's issue date.
Records published after the date they carry fell behind the advancing watermark
and were never collected again -- silently, with every run reporting success.
At least 669 permits were lost across five sources.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.config import settings
from pipeline.config.settings import SCHEMA_PATH
from pipeline.connectors.base import PERMIT_FIELDS, ConnectorResult
from pipeline.ingestion.run_ingestion import compute_since, ingest_jurisdiction, run_ingestion


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "wm.db")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status, connector_type) "
              "VALUES ('scottsdale_az','Scottsdale','AZ','connected','arcgis_hub')")
    c.commit()
    return c


def _add_permit(conn, slug, number, issued_date):
    conn.execute(
        "INSERT INTO permits (jurisdiction, permit_number, issued_date, "
        "first_seen_at, last_updated_at) VALUES (?,?,?,?,?)",
        (slug, number, issued_date, "2026-09-01", "2026-09-01"),
    )
    conn.commit()


def _permit(**kw) -> dict:
    d = {k: None for k in PERMIT_FIELDS}
    d["jurisdiction"] = "scottsdale_az"
    d["raw_source_json"] = "{}"
    d.update(kw)
    return d


class RecordingConnector:
    """Captures the watermark it was asked for, and honours it like a source."""

    def __init__(self, available=()):
        self.available = list(available)  # (permit_number, issued_date)
        self.since_seen = None

    def run(self, since=None):
        self.since_seen = since
        records = []
        for number, issued in self.available:
            day = datetime.strptime(issued, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if since is None or day >= since:
                records.append(_permit(permit_number=number, issued_date=issued))
        return ConnectorResult(jurisdiction_slug="scottsdale_az", records=records,
                               source_row_count=len(records))


def _install(monkeypatch, connector):
    monkeypatch.setattr("pipeline.ingestion.run_ingestion.build_connector",
                        lambda slug, ctype: connector)


# ==========================================================================
# compute_since
# ==========================================================================

def test_watermark_comes_from_held_data_not_last_run(conn):
    _add_permit(conn, "scottsdale_az", "P-1", "2026-07-10")
    since = compute_since(conn, "scottsdale_az", "2026-09-15T08:00:00+00:00")

    expected = (datetime(2026, 7, 10, tzinfo=timezone.utc)
                - timedelta(days=settings.INGEST_WATERMARK_LOOKBACK_DAYS))
    assert since == expected
    assert since.year == 2026 and since.month == 6


def test_watermark_ignores_a_far_future_last_synced_at(conn):
    """The old behaviour returned last_synced_at verbatim; that is the bug."""
    _add_permit(conn, "scottsdale_az", "P-1", "2026-07-10")
    since = compute_since(conn, "scottsdale_az", "2027-01-01T00:00:00+00:00")
    assert since.year == 2026 and since.month == 6


def test_lookback_is_configurable_for_catchup(conn):
    _add_permit(conn, "scottsdale_az", "P-1", "2026-07-10")
    since = compute_since(conn, "scottsdale_az", None, lookback_days=400)
    assert since == datetime(2026, 7, 10, tzinfo=timezone.utc) - timedelta(days=400)


def test_first_run_with_no_held_data_falls_back(conn):
    since = compute_since(conn, "scottsdale_az", "2026-09-01T00:00:00+00:00")
    assert since == datetime.fromisoformat("2026-09-01T00:00:00+00:00")


def test_handles_full_iso_timestamps(conn):
    """Mesa stores issued_date as a full timestamp, not a plain day."""
    _add_permit(conn, "scottsdale_az", "P-1", "2026-09-10T00:00:00.000")
    since = compute_since(conn, "scottsdale_az", None)
    assert since == (datetime(2026, 9, 10, tzinfo=timezone.utc)
                     - timedelta(days=settings.INGEST_WATERMARK_LOOKBACK_DAYS))


def test_falls_back_when_held_date_is_unparseable(conn):
    _add_permit(conn, "scottsdale_az", "P-1", "not-a-date")
    since = compute_since(conn, "scottsdale_az", "2026-09-01T00:00:00+00:00")
    assert since == datetime.fromisoformat("2026-09-01T00:00:00+00:00")


# ==========================================================================
# The defect itself
# ==========================================================================

def test_late_published_record_is_collected(conn, monkeypatch):
    """The exact loss: a permit issued before the last run but published after.

    Under the old watermark this record was invisible forever. It must now be
    picked up.
    """
    _add_permit(conn, "scottsdale_az", "OLD-1", "2026-09-10")
    connector = RecordingConnector(available=[
        ("OLD-1", "2026-09-10"),
        ("LATE-1", "2026-09-05"),  # issued earlier, published later
    ])
    _install(monkeypatch, connector)

    run_ingestion(conn)

    numbers = {r["permit_number"] for r in conn.execute(
        "SELECT permit_number FROM permits WHERE jurisdiction='scottsdale_az'")}
    assert "LATE-1" in numbers


def test_old_watermark_would_have_missed_it(conn):
    """Pins why the fix is needed rather than just that it works."""
    _add_permit(conn, "scottsdale_az", "OLD-1", "2026-09-10")
    last_run = "2026-09-14T08:00:00+00:00"

    old_watermark = datetime.fromisoformat(last_run)
    late_record = datetime(2026, 9, 5, tzinfo=timezone.utc)
    assert late_record < old_watermark          # skipped by the old rule

    assert late_record >= compute_since(conn, "scottsdale_az", last_run)


def test_scottsdale_sized_gap_is_recovered(conn, monkeypatch):
    """Two months of backlog, the shape of the real scottsdale_az loss."""
    _add_permit(conn, "scottsdale_az", "HELD-1", "2026-07-10")
    backlog = [(f"GAP-{i}", f"2026-07-{i:02d}") for i in range(11, 31)]
    connector = RecordingConnector(available=[("HELD-1", "2026-07-10")] + backlog)
    _install(monkeypatch, connector)

    run_ingestion(conn)

    recovered = conn.execute(
        "SELECT COUNT(*) FROM permits WHERE jurisdiction='scottsdale_az' "
        "AND permit_number LIKE 'GAP-%'").fetchone()[0]
    assert recovered == len(backlog)


def test_watermark_is_recorded_in_telemetry(conn, monkeypatch):
    _add_permit(conn, "scottsdale_az", "P-1", "2026-07-10")
    _install(monkeypatch, RecordingConnector())
    run_ingestion(conn)

    row = conn.execute(
        "SELECT requested_since FROM ingestion_runs "
        "WHERE jurisdiction_slug='scottsdale_az'").fetchone()
    assert row["requested_since"].startswith("2026-06")


def test_morning_refresh_path_uses_the_same_watermark(conn, monkeypatch):
    _add_permit(conn, "scottsdale_az", "P-1", "2026-07-10")
    connector = RecordingConnector()
    _install(monkeypatch, connector)

    ingest_jurisdiction(conn, "scottsdale_az", "arcgis_hub",
                        "2026-09-15T08:00:00+00:00")

    assert connector.since_seen.month == 6


def test_refetched_unchanged_records_do_not_churn(conn, monkeypatch):
    """A wide lookback re-reads records every run; that must stay free."""
    connector = RecordingConnector(available=[("P-1", "2026-09-10")])
    _install(monkeypatch, connector)

    ingest_jurisdiction(conn, "scottsdale_az", "arcgis_hub", None)
    first = conn.execute(
        "SELECT last_updated_at FROM permits WHERE permit_number='P-1'").fetchone()[0]

    stats = ingest_jurisdiction(conn, "scottsdale_az", "arcgis_hub", None)
    second = conn.execute(
        "SELECT last_updated_at FROM permits WHERE permit_number='P-1'").fetchone()[0]

    assert stats["unchanged"] == 1
    assert first == second
