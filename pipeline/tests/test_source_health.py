"""Data platform Phase 2 — ingestion telemetry and source health.

Covers the per-run telemetry columns, error classification, and the health
rollup that turns a scatter of individual run rows into a verdict.

The guarantees that matter here:
  * a flaky source that always recovers is still reported as degraded
  * a source returning 200-with-nothing is eventually reported as silent
  * a quiet city is NOT reported as unhealthy (the false-alarm trap)
  * telemetry never blocks or breaks ingestion
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.config import settings
from pipeline.config.settings import SCHEMA_PATH
from pipeline.connectors.base import PERMIT_FIELDS, ConnectorNotConfiguredError, ConnectorResult
from pipeline.ingestion.run_ingestion import (
    classify_error,
    ingest_jurisdiction,
    run_ingestion,
)
from pipeline.telemetry import source_health as sh

NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / "health.db")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for slug, name in (("gilbert_az", "Gilbert"), ("phoenix_az", "Phoenix")):
        c.execute(
            "INSERT INTO jurisdictions (slug, name, state, status, connector_type, "
            "endpoint_url) VALUES (?,?,'AZ','connected','arcgis_hub','https://x.test/y')",
            (slug, name),
        )
    c.commit()
    return c


def _run(conn, slug, *, days_ago, status="success", fetched=0, error_type=None,
         error_message=None):
    started = (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO ingestion_runs (jurisdiction_slug, run_started_at, run_finished_at, "
        "records_fetched, status, error_type, error_message) VALUES (?,?,?,?,?,?,?)",
        (slug, started, started, fetched, status, error_type, error_message),
    )
    conn.commit()


def _permit(**kw) -> dict:
    d = {k: None for k in PERMIT_FIELDS}
    d["jurisdiction"] = "phoenix_az"
    d["raw_source_json"] = '{"a": 1}'
    d.update(kw)
    return d


class FakeConnector:
    def __init__(self, slug, records=None, raises=None):
        self.slug = slug
        self.records = records or []
        self.raises = raises

    def run(self, since=None):
        if self.raises:
            raise self.raises
        out = []
        for r in self.records:
            m = dict(r)
            m["jurisdiction"] = self.slug
            m.setdefault("raw_source_json", "{}")
            out.append(m)
        return ConnectorResult(jurisdiction_slug=self.slug, records=out,
                               source_row_count=len(out))


def _install(monkeypatch, mapping):
    def _build(slug, connector_type):
        if slug in mapping:
            return mapping[slug]
        raise ConnectorNotConfiguredError(slug)
    monkeypatch.setattr("pipeline.ingestion.run_ingestion.build_connector", _build)


# ==========================================================================
# Error classification
# ==========================================================================

def test_classifies_http_status_families():
    import requests

    def http_error(code):
        response = requests.Response()
        response.status_code = code
        return requests.exceptions.HTTPError(f"{code} error", response=response)

    assert classify_error(http_error(500)) == ("http_5xx", 500)
    assert classify_error(http_error(503)) == ("http_5xx", 503)
    assert classify_error(http_error(404)) == ("http_4xx", 404)
    assert classify_error(http_error(429)) == ("rate_limited", 429)


def test_classifies_transport_failures():
    import requests

    assert classify_error(requests.exceptions.Timeout("slow"))[0] == "timeout"
    assert classify_error(requests.exceptions.ConnectionError("refused"))[0] == "connection"


def test_classifies_configuration_failure():
    assert classify_error(ConnectorNotConfiguredError("nope")) == ("not_configured", None)


def test_classifies_unknown_and_none():
    assert classify_error(ValueError("???")) == ("unknown", None)
    assert classify_error(None) == (None, None)


# ==========================================================================
# Health states
# ==========================================================================

def test_no_runs_is_unknown(conn):
    health = sh.evaluate(conn, "gilbert_az", NOW)
    assert health["health_state"] == sh.UNKNOWN


def test_recent_successes_are_healthy(conn):
    for d in (0, 1, 2):
        _run(conn, "gilbert_az", days_ago=d, fetched=50)
    health = sh.evaluate(conn, "gilbert_az", NOW)
    assert health["health_state"] == sh.HEALTHY
    assert health["records_7d"] == 150
    assert health["success_rate_7d"] == 1.0


def test_sustained_failure_streak_is_failing(conn):
    _run(conn, "gilbert_az", days_ago=4, fetched=97)
    for d in (0, 1, 2):
        _run(conn, "gilbert_az", days_ago=d, status="error", error_type="http_5xx")
    health = sh.evaluate(conn, "gilbert_az", NOW)

    assert health["health_state"] == sh.FAILING
    assert health["consecutive_failures"] == 3
    assert health["last_error_type"] == "http_5xx"
    assert "http_5xx" in health["detail"]


def test_single_recent_failure_is_degraded_not_failing(conn):
    for d in (1, 2, 3):
        _run(conn, "gilbert_az", days_ago=d, fetched=10)
    _run(conn, "gilbert_az", days_ago=0, status="error", error_type="timeout")
    health = sh.evaluate(conn, "gilbert_az", NOW)

    assert health["health_state"] == sh.DEGRADED
    assert health["consecutive_failures"] == 1


def test_intermittent_source_that_always_recovers_is_still_degraded(conn):
    """The gilbert_az pattern: fails, recovers, fails again. Each run looks
    fine in isolation, which is exactly why it went unnoticed."""
    _run(conn, "gilbert_az", days_ago=6, status="error", error_type="connection")
    _run(conn, "gilbert_az", days_ago=5, fetched=14)
    _run(conn, "gilbert_az", days_ago=2, status="error", error_type="connection")
    _run(conn, "gilbert_az", days_ago=0, fetched=97)

    health = sh.evaluate(conn, "gilbert_az", NOW)
    assert health["consecutive_failures"] == 0  # last run succeeded
    assert health["failures_7d"] == 2
    assert health["health_state"] == sh.DEGRADED


def test_successful_but_empty_runs_become_silent(conn):
    """A withdrawn feed or broken incremental filter returns 200 with nothing,
    which is indistinguishable from 'no new permits' until it persists."""
    _run(conn, "phoenix_az", days_ago=40, fetched=500)
    for d in range(0, 30, 3):
        _run(conn, "phoenix_az", days_ago=d, fetched=0)

    health = sh.evaluate(conn, "phoenix_az", NOW)
    assert health["health_state"] == sh.SILENT
    assert health["days_since_last_data"] >= settings.SOURCE_SILENT_DAYS


def test_briefly_empty_source_is_not_silent(conn):
    """Two quiet days is normal for an incremental source."""
    _run(conn, "phoenix_az", days_ago=2, fetched=30)
    _run(conn, "phoenix_az", days_ago=1, fetched=0)
    _run(conn, "phoenix_az", days_ago=0, fetched=0)
    assert sh.evaluate(conn, "phoenix_az", NOW)["health_state"] == sh.HEALTHY


def test_newly_connected_source_with_no_data_is_not_silent(conn):
    _run(conn, "phoenix_az", days_ago=1, fetched=0)
    health = sh.evaluate(conn, "phoenix_az", NOW)
    assert health["health_state"] == sh.HEALTHY
    assert "recently connected" in health["detail"]


def test_a_quiet_city_is_not_reported_unhealthy(conn):
    """The false-alarm trap: old permit dates are reported, never diagnostic.
    A city that is not issuing permits is not a broken source."""
    conn.execute(
        "INSERT INTO permits (jurisdiction, permit_number, issued_date, "
        "first_seen_at, last_updated_at) VALUES ('phoenix_az','P-1','2024-01-01',?,?)",
        ("2024-01-01", "2024-01-01"),
    )
    conn.commit()
    for d in (0, 1, 2):
        _run(conn, "phoenix_az", days_ago=d, fetched=5)

    health = sh.evaluate(conn, "phoenix_az", NOW)
    assert health["health_state"] == sh.HEALTHY
    assert health["newest_source_date"] == "2024-01-01"
    assert health["days_since_newest_source"] > 500


# ==========================================================================
# Snapshots
# ==========================================================================

def test_snapshot_is_recorded_for_every_connected_source(conn):
    _run(conn, "gilbert_az", days_ago=0, status="error", error_type="http_5xx")
    results = sh.record_snapshot(conn, NOW)

    assert len(results) == 2
    rows = list(conn.execute("SELECT * FROM source_health_snapshot"))
    assert len(rows) == 2
    gilbert = next(r for r in rows if r["jurisdiction_slug"] == "gilbert_az")
    assert gilbert["health_state"] == sh.DEGRADED
    assert gilbert["last_error_type"] == "http_5xx"


def test_snapshots_accumulate_as_history(conn):
    _run(conn, "gilbert_az", days_ago=0, fetched=5)
    sh.record_snapshot(conn, NOW)
    sh.record_snapshot(conn, NOW + timedelta(days=1))

    assert len(sh.history(conn, "gilbert_az")) == 2
    assert len(list(conn.execute("SELECT * FROM source_health_snapshot"))) == 4


def test_worst_sources_sort_first(conn):
    for d in (0, 1, 2):
        _run(conn, "gilbert_az", days_ago=d, status="error", error_type="http_5xx")
    _run(conn, "phoenix_az", days_ago=0, fetched=10)

    results = sh.evaluate_all(conn, NOW)
    assert results[0]["jurisdiction_slug"] == "gilbert_az"
    assert results[0]["health_state"] == sh.FAILING


def test_snapshot_failure_cannot_break_the_caller(conn, monkeypatch):
    def boom(*a, **kw):
        raise sqlite3.OperationalError("no such table")
    monkeypatch.setattr(sh, "record_snapshot", boom)
    assert sh.record_snapshot_safe(conn) == []


# ==========================================================================
# Telemetry captured by real ingestion
# ==========================================================================

def test_successful_run_records_full_telemetry(conn, monkeypatch):
    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", [_permit(permit_number="A-1")]),
        "gilbert_az": FakeConnector("gilbert_az", []),
    })
    run_ingestion(conn)

    row = conn.execute(
        "SELECT * FROM ingestion_runs WHERE jurisdiction_slug='phoenix_az'"
    ).fetchone()
    assert row["status"] == "success"
    assert row["connector_type"] == "arcgis_hub"
    assert row["requested_since"] is not None
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0
    assert row["raw_new"] == 1
    assert row["raw_batch_id"] is not None
    assert row["source_rows"] == 1


def test_failed_run_records_classified_error(conn, monkeypatch):
    import requests

    response = requests.Response()
    response.status_code = 500
    error = requests.exceptions.HTTPError("500 Server Error", response=response)
    _install(monkeypatch, {
        "gilbert_az": FakeConnector("gilbert_az", raises=error),
        "phoenix_az": FakeConnector("phoenix_az", []),
    })
    run_ingestion(conn)

    row = conn.execute(
        "SELECT * FROM ingestion_runs WHERE jurisdiction_slug='gilbert_az'"
    ).fetchone()
    assert row["status"] == "error"
    assert row["error_type"] == "http_5xx"
    assert row["http_status"] == 500


def test_ingestion_records_a_health_snapshot(conn, monkeypatch):
    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", []),
        "gilbert_az": FakeConnector("gilbert_az", []),
    })
    run_ingestion(conn)
    assert conn.execute(
        "SELECT COUNT(*) FROM source_health_snapshot"
    ).fetchone()[0] == 2


def test_morning_refresh_path_records_telemetry(conn, monkeypatch):
    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", [_permit(permit_number="A-1")]),
    })
    ingest_jurisdiction(conn, "phoenix_az", "arcgis_hub", None)

    row = conn.execute(
        "SELECT * FROM ingestion_runs WHERE jurisdiction_slug='phoenix_az'"
    ).fetchone()
    assert row["connector_type"] == "arcgis_hub"
    assert row["raw_new"] == 1
    assert row["retries"] == 0
    assert row["records_unchanged"] == 0


def test_retry_budget_is_recorded_on_failure(conn, monkeypatch):
    class Timeoutish(Exception):
        """Name contains 'timeout' -> classified retryable."""

    _install(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", raises=Timeoutish("slow")),
    })
    stats = ingest_jurisdiction(conn, "phoenix_az", "arcgis_hub", None,
                                attempts=3, sleep=lambda s: None)

    assert stats["status"] == "failed"
    row = conn.execute(
        "SELECT * FROM ingestion_runs WHERE jurisdiction_slug='phoenix_az'"
    ).fetchone()
    assert row["error_type"] == "timeout"
    assert row["retries"] == 2


def test_telemetry_survives_a_database_without_phase2_columns(tmp_path, monkeypatch):
    """An older database must still record that a run happened."""
    c = sqlite3.connect(tmp_path / "old.db")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for column in ("connector_type", "requested_since", "duration_ms", "raw_new"):
        c.execute(f"ALTER TABLE ingestion_runs DROP COLUMN {column}")
    c.execute("INSERT INTO jurisdictions (slug, name, state, status, connector_type) "
              "VALUES ('phoenix_az','Phoenix','AZ','connected','arcgis_hub')")
    c.commit()

    _install(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az", [])})
    run_ingestion(c)

    assert c.execute(
        "SELECT COUNT(*) FROM ingestion_runs WHERE jurisdiction_slug='phoenix_az'"
    ).fetchone()[0] == 1
