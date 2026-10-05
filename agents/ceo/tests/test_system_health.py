"""Deterministic morning system health. A model is not part of these results."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from agents.ceo.analytics.guard import AnalyticsError
from agents.ceo.analytics.health import (
    LABELS,
    evaluate_system_health,
    load_health_record,
    write_health,
)
from agents.ceo.analytics.publish import publish_owner_brief
from agents.ceo.analytics.serve import load_owner_brief_view
from pipeline.config.settings import SCHEMA_PATH


CHECKED = "2026-10-05T12:00:00+00:00"
SECRET_MARKERS = ("password", "token", "cookie", "secret", "api_key", "sk_live")


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def _jurisdiction(conn, slug="phoenix_az") -> None:
    conn.execute(
        "INSERT INTO jurisdictions (slug, name, state, status, connector_type) VALUES (?, ?, 'AZ', 'connected', 'arcgis_hub')",
        (slug, slug),
    )


def _refresh(conn, *, status="succeeded", completed=CHECKED, received=0, started=None) -> None:
    started = CHECKED if started is None else started
    conn.execute(
        """
        INSERT INTO pipeline_runs (
            run_type, status, started_at, completed_at, records_received, created_at
        ) VALUES ('morning_refresh', ?, ?, ?, ?, ?)
        """,
        (status, started, completed, received, started),
    )


def _snap(conn, slug, captured, records, *, days=2, state="healthy") -> None:
    conn.execute(
        """
        INSERT INTO source_health_snapshot (
            captured_at, jurisdiction_slug, health_state, records_7d,
            days_since_newest_source, newest_source_date
        ) VALUES (?, ?, ?, ?, ?, '2026-10-03')
        """,
        (captured, slug, state, records, days),
    )


def _healthy_db(tmp_path: Path) -> Path:
    path = tmp_path / "health.db"
    conn = _open(path)
    _jurisdiction(conn)
    _refresh(conn)
    _snap(conn, "phoenix_az", "2026-10-05T11:00:00+00:00", 20)
    conn.commit()
    conn.close()
    return path


def _eval(path: Path, **kwargs):
    return evaluate_system_health(path, checked_at=CHECKED, **kwargs)


def _one(health, check_id):
    return next(item for item in health["checks"] if item["id"] == check_id)


def _feed_issue(health, source="phoenix_az"):
    issues = _one(health, "feeds")["evidence"]["issues"]
    return next(item for item in issues if item["source"] == source)


def test_all_systems_healthy(tmp_path):
    health = _eval(_healthy_db(tmp_path))
    assert health["overall_status"] == "healthy"
    assert health["label"] == "ALL SYSTEMS HEALTHY"
    assert health["owner_alert_required"] is False
    assert health["alert_severity"] == "healthy"
    assert health["incident_count"] == 0
    assert health["execute"] is False
    assert health["scope"] == {"kind": "owner", "organization_id": None}
    billing = _one(health, "billing")
    assert billing["status"] == "not_applicable"
    assert billing["evidence"]["state"] == "NOT_ENABLED"
    assert health["components"]["billing"]["status"] == "not_applicable"
    assert all(item["automatic_remediation_permitted"] is False for item in health["incidents"])


def test_failed_refresh_is_critical_and_does_not_publish_a_brief(tmp_path, monkeypatch):
    path = tmp_path / "health.db"
    conn = _open(path)
    _jurisdiction(conn)
    _refresh(conn, status="failed")
    _snap(conn, "phoenix_az", "2026-10-05T11:00:00+00:00", 20)
    conn.commit()
    conn.close()
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo"))
    result = publish_owner_brief(db_path=path, refresh={
        "run_id": 4, "status": "failed", "completed_at": CHECKED,
    })
    assert result["brief_status"] == "skipped"
    assert result["health"]["overall_status"] == "critical"
    assert result["health"]["label"] == "CRITICAL — ACTION REQUIRED"
    assert result["health"]["owner_alert_required"] is True
    assert _one(result["health"], "morning_refresh")["status"] == "critical"
    assert not (tmp_path / "ceo" / "intelligence" / "latest_daily_brief.json").exists()
    assert (tmp_path / "ceo" / "intelligence" / "latest_health_status.json").is_file()


def test_partial_refresh_is_a_warning(tmp_path):
    path = tmp_path / "health.db"
    conn = _open(path)
    _jurisdiction(conn)
    _refresh(conn, status="partial")
    _snap(conn, "phoenix_az", "2026-10-05T11:00:00+00:00", 20)
    conn.commit()
    conn.close()
    health = _eval(path)
    assert health["overall_status"] == "warning"
    assert health["label"] == "WARNING — REVIEW NEEDED"
    assert health["owner_alert_required"] is True
    assert _one(health, "morning_refresh")["status"] == "warning"
    assert health["overall_status"] != "critical"


def test_newer_failure_is_not_hidden_by_an_older_success(tmp_path):
    path = tmp_path / "health.db"
    conn = _open(path)
    _jurisdiction(conn)
    _refresh(conn, status="succeeded", completed="2026-10-04T12:00:00+00:00")
    _refresh(conn, status="failed", completed=CHECKED)
    _snap(conn, "phoenix_az", "2026-10-05T11:00:00+00:00", 20)
    conn.commit()
    conn.close()
    health = _eval(path)
    refresh = _one(health, "morning_refresh")
    assert health["overall_status"] == "critical"
    assert refresh["evidence"]["status"] == "failed"
    assert refresh["evidence"]["last_known_success_at"] == "2026-10-04T12:00:00+00:00"
    assert health["label"] != "ALL SYSTEMS HEALTHY"


def test_stale_feed_is_a_warning(tmp_path):
    path = _healthy_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE source_health_snapshot SET days_since_newest_source = 80, records_7d = 12"
    )
    conn.commit()
    conn.close()
    health = _eval(path)
    issue = _feed_issue(health)
    assert health["overall_status"] == "warning"
    assert issue["claim_class"] == "FACT"
    assert "stale" in issue["fact"]
    assert health["owner_alert_required"] is True


def test_thin_feed_is_a_warning(tmp_path):
    path = _healthy_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE source_health_snapshot SET days_since_newest_source = 80, records_7d = 0"
    )
    conn.commit()
    conn.close()
    health = _eval(path)
    issue = _feed_issue(health)
    assert "thin" in issue["fact"]
    assert issue["severity"] == "warning"
    assert health["overall_status"] == "warning"


def test_zero_with_baseline_is_an_inference(tmp_path):
    path = tmp_path / "health.db"
    conn = _open(path)
    _jurisdiction(conn)
    _refresh(conn)
    for day, records in (("01", 8), ("02", 8), ("03", 9), ("04", 8)):
        _snap(conn, "phoenix_az", f"2026-10-{day}T00:00:00+00:00", records)
    _snap(conn, "phoenix_az", "2026-10-05T11:00:00+00:00", 0)
    conn.commit()
    conn.close()
    health = _eval(path)
    issue = _feed_issue(health)
    assert issue["claim_class"] == "INFERENCE"
    assert issue["severity"] == "warning"
    assert "records_7d=0" in issue["fact"]
    assert "broken" not in issue["fact"]
    assert health["overall_status"] == "warning"
    incident = next(item for item in health["incidents"] if item["source"] == "phoenix_az")
    assert incident["automatic_remediation_permitted"] is False
    assert incident["claim_class"] == "INFERENCE"


def test_zero_without_baseline_stays_unknown_and_healthy_overall(tmp_path):
    path = tmp_path / "health.db"
    conn = _open(path)
    _jurisdiction(conn)
    _refresh(conn)
    _snap(conn, "phoenix_az", "2026-10-05T11:00:00+00:00", 0)
    conn.commit()
    conn.close()
    health = _eval(path)
    issue = _feed_issue(health)
    assert issue["severity"] == "unknown"
    assert issue["claim_class"] == "UNKNOWN"
    assert _one(health, "feeds")["status"] == "unknown"
    assert health["overall_status"] == "healthy"
    assert health["owner_alert_required"] is False
    assert health["incident_count"] == 0


def test_large_volume_decline_with_baseline_is_a_warning(tmp_path):
    path = tmp_path / "health.db"
    conn = _open(path)
    _jurisdiction(conn)
    _refresh(conn)
    for day in ("01", "02", "03", "04"):
        _snap(conn, "phoenix_az", f"2026-10-{day}T00:00:00+00:00", 40)
    _snap(conn, "phoenix_az", "2026-10-05T11:00:00+00:00", 4)
    conn.commit()
    conn.close()
    health = _eval(path)
    issue = _feed_issue(health)
    assert issue["claim_class"] == "INFERENCE"
    assert issue["severity"] == "warning"
    assert "Median" in issue["fact"]
    assert health["overall_status"] == "warning"
    assert health["owner_alert_required"] is True


def test_missing_database_is_critical(tmp_path):
    health = _eval(tmp_path / "missing.db")
    assert health["overall_status"] == "critical"
    assert _one(health, "database")["status"] == "critical"
    assert health["owner_alert_required"] is True
    assert health["label"] != "ALL SYSTEMS HEALTHY"
    assert _one(health, "billing")["status"] == "not_applicable"


def test_unreadable_database_is_critical(tmp_path):
    path = tmp_path / "broken.db"
    path.write_text("this is not a sqlite database", encoding="utf-8")
    health = _eval(path)
    assert health["overall_status"] == "critical"
    assert _one(health, "database")["status"] == "critical"
    assert "failed" in _one(health, "database")["summary"] or "could not be opened" in _one(health, "database")["summary"]
    assert health["overall_status"] != "healthy"


def test_missing_critical_table_is_critical(tmp_path):
    path = _healthy_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE sessions")
    conn.commit()
    conn.close()
    health = _eval(path)
    schema = _one(health, "schema")
    assert schema["status"] == "critical"
    assert "sessions" in schema["evidence"]["missing"]
    assert health["overall_status"] == "critical"
    assert health["label"] != "ALL SYSTEMS HEALTHY"


def test_downstream_query_failure_is_critical(tmp_path, monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AnalyticsError("opportunities unavailable")

    monkeypatch.setattr("agents.ceo.analytics.health.get_top_opportunities", _boom)
    health = _eval(_healthy_db(tmp_path))
    assert _one(health, "downstream_analytics")["status"] == "critical"
    assert health["overall_status"] == "critical"
    assert health["owner_alert_required"] is True


def test_brief_generation_failure_keeps_the_prior_brief_and_is_critical(tmp_path, monkeypatch):
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo"))
    path = _healthy_db(tmp_path)
    first = publish_owner_brief(db_path=path, refresh={
        "run_id": 7, "status": "succeeded", "completed_at": CHECKED,
    })
    assert first["brief_status"] == "succeeded"
    assert first["health"]["overall_status"] == "healthy"

    def _boom(**_kwargs):
        raise RuntimeError("generator failed")

    monkeypatch.setattr("agents.ceo.analytics.publish.build_daily_brief", _boom)
    second = publish_owner_brief(db_path=path, refresh={
        "run_id": 8, "status": "succeeded", "completed_at": CHECKED,
    })
    assert second["brief_status"] == "failed"
    assert second["health"]["overall_status"] == "critical"
    assert _one(second["health"], "brief_publication")["status"] == "critical"
    stored = json.loads((tmp_path / "ceo" / "intelligence" / "latest_daily_brief.json").read_text(encoding="utf-8"))
    assert stored["provenance"]["refresh_run_id"] == 7
    health_path = tmp_path / "ceo" / "intelligence" / "latest_health_status.json"
    text = health_path.read_text(encoding="utf-8")
    assert "generator failed" in text or "RuntimeError" in text
    for incident in second["health"]["incidents"]:
        assert incident["automatic_remediation_permitted"] is False


def test_health_check_exception_fails_closed(tmp_path, monkeypatch):
    def _boom(_conn):
        raise RuntimeError("password=hunter2")

    monkeypatch.setattr("agents.ceo.analytics.health._schema", _boom)
    health = _eval(_healthy_db(tmp_path))
    assert health["overall_status"] == "unknown"
    assert health["label"] == LABELS["unknown"]
    assert health["label"] == "CHECK INCOMPLETE — REVIEW NEEDED"
    assert health["owner_alert_required"] is True
    assert health["overall_status"] != "healthy"
    dumped = json.dumps(health)
    assert "hunter2" not in dumped
    assert "password" not in dumped
    assert _one(health, "health_check")["evidence"]["error"] == "RuntimeError"


def test_billing_not_enabled_does_not_change_a_healthy_result(tmp_path):
    health = _eval(_healthy_db(tmp_path))
    assert _one(health, "billing")["evidence"]["state"] == "NOT_ENABLED"
    assert health["overall_status"] == "healthy"
    assert health["owner_alert_required"] is False


def test_running_refresh_is_incomplete_and_cannot_be_healthy(tmp_path):
    path = tmp_path / "health.db"
    conn = _open(path)
    _jurisdiction(conn)
    _refresh(conn, status="running", completed=None, started=CHECKED)
    _snap(conn, "phoenix_az", "2026-10-05T11:00:00+00:00", 20)
    conn.commit()
    conn.close()
    health = _eval(path)
    assert _one(health, "morning_refresh")["status"] == "unknown"
    assert _one(health, "morning_refresh")["critical"] is True
    assert health["overall_status"] == "unknown"
    assert health["owner_alert_required"] is True
    assert health["label"] != "ALL SYSTEMS HEALTHY"


def test_malformed_health_file_falls_back_to_the_last_valid_record(tmp_path):
    path = _healthy_db(tmp_path)
    warning = _eval(path)
    # Force a warning record, then hide it behind a malformed latest file.
    warning["overall_status"] = "warning"
    warning["label"] = LABELS["warning"]
    warning["owner_alert_required"] = True
    warning["checks"].append({
        "id": "notice",
        "component": "feeds",
        "status": "warning",
        "critical": False,
        "claim_class": "FACT",
        "summary": "Stored warning for fallback.",
        "rule": "A valid history record is preferred to a malformed latest file.",
        "evidence": {},
    })
    directory = tmp_path / "intelligence"
    write_health(warning, directory)
    (directory / "latest_health_status.json").write_text(
        '{"overall_status":"healthy","password":"hunter2"}',
        encoding="utf-8",
    )
    record, notice = load_health_record(directory)
    assert record["overall_status"] == "warning"
    assert "failed validation" in notice
    assert "hunter2" not in json.dumps(record)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    view = load_owner_brief_view(reports_conn=conn, directory=directory)
    conn.close()
    assert view["available"] is False
    assert view["health"]["overall_status"] == "warning"
    assert view["health"]["current"] is False
    assert "hunter2" not in json.dumps(view)
    assert view["health"]["label"] != "ALL SYSTEMS HEALTHY"


def test_cross_tenant_health_file_is_rejected(tmp_path):
    path = _healthy_db(tmp_path)
    health = _eval(path)
    directory = tmp_path / "intelligence"
    write_health(health, directory)
    latest = directory / "latest_health_status.json"
    payload = json.loads(latest.read_text(encoding="utf-8"))
    payload["scope"] = {"kind": "owner", "organization_id": 2}
    payload["checks"][0]["summary"] = "TENANT-2-ONLY"
    latest.write_text(json.dumps(payload), encoding="utf-8")
    record, notice = load_health_record(directory)
    assert record["scope"]["organization_id"] is None
    assert "TENANT-2-ONLY" not in json.dumps(record)
    assert notice
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    view = load_owner_brief_view(reports_conn=conn, directory=directory)
    conn.close()
    assert view["health"]["scope"]["organization_id"] is None
    assert "TENANT-2-ONLY" not in json.dumps(view)


def test_health_artifact_has_no_secrets_and_never_permits_remediation(tmp_path, monkeypatch):
    path = _healthy_db(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO organizations (name, slug, is_active, created_at, updated_at) VALUES ('CorridorIQ', 'corridoriq', 1, ?, ?)",
        (CHECKED, CHECKED),
    )
    conn.execute(
        """
        INSERT INTO users (
            organization_id, email, normalized_email, password_hash, created_at, updated_at
        ) VALUES (1, 'owner@example.com', 'owner@example.com', 'super-secret-hash-value', ?, ?)
        """,
        (CHECKED, CHECKED),
    )
    conn.commit()
    conn.close()
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo"))
    result = publish_owner_brief(db_path=path, refresh={
        "run_id": 3, "status": "succeeded", "completed_at": CHECKED,
    })
    text = (tmp_path / "ceo" / "intelligence" / "latest_health_status.json").read_text(encoding="utf-8")
    run_id = result["health"]["refresh_run_id"]
    assert isinstance(run_id, int)
    history = tmp_path / "ceo" / "intelligence" / "history" / f"health-refresh-{run_id:06d}.json"
    assert history.is_file()
    assert "super-secret-hash-value" not in text
    lowered = text.lower()
    for marker in SECRET_MARKERS:
        assert marker not in lowered
    assert result["health"]["execute"] is False
    for incident in result["health"]["incidents"]:
        assert incident["automatic_remediation_permitted"] is False
    tampered = json.loads(text)
    tampered["incidents"] = [{
        "id": "x",
        "severity": "warning",
        "automatic_remediation_permitted": True,
    }]
    (tmp_path / "ceo" / "intelligence" / "latest_health_status.json").write_text(
        json.dumps(tampered), encoding="utf-8",
    )
    record, notice = load_health_record(tmp_path / "ceo" / "intelligence")
    assert "automatic_remediation_permitted" not in json.dumps(record) or all(
        item.get("automatic_remediation_permitted") is False for item in record["incidents"]
    )
    assert notice


def test_production_runtime_blocks_a_non_production_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRIDORIQ_ENV", "development")
    health = _eval(_healthy_db(tmp_path))
    check = _one(health, "production_runtime")
    assert check["status"] == "critical" and check["critical"] is True
    assert "environment_not_production" in check["evidence"]["problems"]
    assert health["overall_status"] == "critical"
    assert health["execute"] is False


def test_production_runtime_blocks_a_disabled_pilot(tmp_path, monkeypatch):
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.delenv("CORRIDORIQ_PILOT_ROOT", raising=False)
    health = _eval(_healthy_db(tmp_path))
    check = _one(health, "production_runtime")
    assert "pilot_disabled" in check["evidence"]["problems"]
    assert health["overall_status"] == "critical"


def test_production_runtime_blocks_an_unavailable_pilot_root(tmp_path, monkeypatch):
    missing = tmp_path / "missing-pilot-root"
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.setenv("CORRIDORIQ_PILOT_ROOT", str(missing))
    health = _eval(_healthy_db(tmp_path))
    check = _one(health, "production_runtime")
    assert "pilot_root_unavailable" in check["evidence"]["problems"]
    assert health["overall_status"] == "critical"
    assert not missing.exists()


def test_production_runtime_blocks_a_misconfigured_pilot_root(tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("x", encoding="utf-8")
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.setenv("CORRIDORIQ_PILOT_ROOT", str(blocked))
    health = _eval(_healthy_db(tmp_path))
    assert "pilot_misconfigured" in _one(health, "production_runtime")["evidence"]["problems"]
    assert health["overall_status"] == "critical"


def test_owner_alert_rules():
    cases = {
        "healthy": False,
        "warning": True,
        "critical": True,
        "unknown": True,
    }
    for status, alert in cases.items():
        assert (status in {"warning", "critical", "unknown"}) is alert
        assert LABELS[status]
