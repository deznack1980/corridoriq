"""Sprint 6.3 — automated morning data pipeline tests.

Covers: full success, partial jurisdiction failure, total failure, retry
behavior, duplicate-run prevention, idempotent rerun, updated lifecycle
records, submitted-only ingestion, dashboard freshness status, admin-only
manual trigger, and log / run-record creation.

No scoring weights or the 60-point threshold are touched here.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.config import settings
from pipeline.config.settings import SCHEMA_PATH
from pipeline.connectors.base import PERMIT_FIELDS, ConnectorNotConfiguredError, ConnectorResult
from pipeline import pipeline_runs


# --------------------------------------------------------------------------
# Fake connectors (no network).
# --------------------------------------------------------------------------

class TransientNetworkError(Exception):
    """Name contains 'network' -> classified retryable."""


class InvalidRequestError(Exception):
    """Name contains 'invalid' -> classified non-retryable."""


def _permit(**kw) -> dict:
    d = {k: None for k in PERMIT_FIELDS}
    d.update(kw)
    return d


class FakeConnector:
    def __init__(self, slug, records=None, errors=None, fail_seq=None):
        self.slug = slug
        self.records = records or []
        self.errors = errors or []
        self.fail_seq = list(fail_seq or [])
        self.calls = 0

    def run(self, since=None):
        self.calls += 1
        if self.fail_seq:
            raise self.fail_seq.pop(0)
        recs = []
        for r in self.records:
            m = dict(r)
            m["jurisdiction"] = self.slug
            m.setdefault("raw_source_json", "{}")
            recs.append(m)
        return ConnectorResult(jurisdiction_slug=self.slug, records=recs, errors=list(self.errors))


def _today_str():
    return datetime.now(timezone.utc).date().isoformat()


# --------------------------------------------------------------------------
# Fixtures.
# --------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(tmp_path / "mr.db")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for slug, name in (("phoenix_az", "Phoenix"), ("mesa_az", "Mesa")):
        c.execute("INSERT INTO jurisdictions (slug, name, state, status) "
                  "VALUES (?,?, 'AZ','connected')", (slug, name))
    seed_auth(c)
    c.commit()

    # Keep heavy downstream stages out of orchestration unit tests; the analysis
    # stage stays real so we can assert incremental scoring / lifecycle.
    monkeypatch.setattr(pipeline_runs, "_refresh_company_intelligence", lambda conn: None)
    monkeypatch.setattr(pipeline_runs, "_refresh_exports", lambda conn: None)
    # Redirect all output dirs into the tmp workspace.
    monkeypatch.setattr(settings, "DATA_EXPORTS_DIR", tmp_path / "exports")
    monkeypatch.setattr(settings, "REPORTS_GENERATED_DIR", tmp_path / "reports")
    monkeypatch.setattr(settings, "MORNING_REFRESH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo-out"))
    return c


def _install_connectors(monkeypatch, mapping: dict):
    def _build(slug, connector_type):
        if slug in mapping:
            return mapping[slug]
        raise ConnectorNotConfiguredError(f"no connector for {slug}")
    monkeypatch.setattr("pipeline.ingestion.run_ingestion.build_connector", _build)


def _run(conn, **kw):
    return pipeline_runs.run_morning_refresh(conn, sleep=lambda s: None, **kw)


# --------------------------------------------------------------------------
# 1. Successful complete run.
# --------------------------------------------------------------------------

def test_successful_complete_run(conn, monkeypatch):
    _install_connectors(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", records=[
            _permit(permit_number="PHX-1", status="Applied", filed_date=_today_str(),
                    description="New commercial building", permit_type="Building"),
        ]),
        "mesa_az": FakeConnector("mesa_az", records=[
            _permit(permit_number="MSA-1", status="Issued", issued_date=_today_str(),
                    description="Tenant improvement plumbing", permit_type="Plumbing"),
        ]),
    })
    summary = _run(conn)
    assert summary["status"] == "succeeded"
    assert summary["jurisdictions_attempted"] == 2
    assert summary["jurisdictions_succeeded"] == 2
    assert summary["jurisdictions_failed"] == 0
    assert summary["records_received"] == 2
    assert summary["records_created"] == 2
    # A run record exists and matches.
    row = conn.execute("SELECT * FROM pipeline_runs WHERE id=?", (summary["run_id"],)).fetchone()
    assert row["status"] == "succeeded"
    assert row["records_created"] == 2
    # Projects were scored for the newly ingested permits (queues get fed).
    n = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    assert n == 2


# --------------------------------------------------------------------------
# 2. Partial jurisdiction failure — one bad source must not stop the others.
# --------------------------------------------------------------------------

def test_partial_jurisdiction_failure(conn, monkeypatch):
    _install_connectors(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", records=[
            _permit(permit_number="PHX-1", status="Applied", filed_date=_today_str()),
        ]),
        "mesa_az": FakeConnector("mesa_az", fail_seq=[InvalidRequestError("bad query")]),
    })
    summary = _run(conn)
    assert summary["status"] == "partial"
    assert summary["jurisdictions_succeeded"] == 1
    assert summary["jurisdictions_failed"] == 1
    # The good jurisdiction still ingested.
    assert summary["records_created"] == 1
    # The failure is recorded, not raised.
    slugs = {j["slug"]: j for j in summary["jurisdictions"]}
    assert slugs["mesa_az"]["status"] == "Failed"


# --------------------------------------------------------------------------
# 3. Total failure.
# --------------------------------------------------------------------------

def test_total_failure(conn, monkeypatch):
    _install_connectors(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", fail_seq=[InvalidRequestError("nope")]),
        "mesa_az": FakeConnector("mesa_az", fail_seq=[InvalidRequestError("nope")]),
    })
    summary = _run(conn)
    assert summary["status"] == "failed"
    assert summary["jurisdictions_succeeded"] == 0
    assert summary["jurisdictions_failed"] == 2


# --------------------------------------------------------------------------
# 4. Retry behavior — transient errors retried, auth/invalid errors not.
# --------------------------------------------------------------------------

def test_retry_on_transient_then_success(conn, monkeypatch):
    from pipeline.ingestion.run_ingestion import ingest_jurisdiction
    fc = FakeConnector("phoenix_az",
                       records=[_permit(permit_number="PHX-1", status="Applied",
                                        filed_date=_today_str())],
                       fail_seq=[TransientNetworkError("timeout"),
                                 TransientNetworkError("timeout")])
    _install_connectors(monkeypatch, {"phoenix_az": fc})
    stats = ingest_jurisdiction(conn, "phoenix_az", None, None, sleep=lambda s: None)
    assert stats["status"] == "success"
    assert stats["retries"] == 2
    assert fc.calls == 3  # 2 failures + 1 success


def test_no_retry_on_invalid_request(conn, monkeypatch):
    from pipeline.ingestion.run_ingestion import ingest_jurisdiction
    fc = FakeConnector("phoenix_az",
                       fail_seq=[InvalidRequestError("400"), InvalidRequestError("400")])
    _install_connectors(monkeypatch, {"phoenix_az": fc})
    stats = ingest_jurisdiction(conn, "phoenix_az", None, None, sleep=lambda s: None)
    assert stats["status"] == "failed"
    assert fc.calls == 1  # gave up immediately, no retry


def test_retry_classifier():
    from pipeline.ingestion.run_ingestion import is_retryable_error
    assert is_retryable_error(TransientNetworkError("x")) is True
    assert is_retryable_error(InvalidRequestError("x")) is False
    assert is_retryable_error(ConnectorNotConfiguredError("x")) is False


# --------------------------------------------------------------------------
# 5. Duplicate-run prevention.
# --------------------------------------------------------------------------

def test_duplicate_run_prevented(conn, monkeypatch):
    _install_connectors(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az")})
    now = pipeline_runs._now()
    conn.execute(
        "INSERT INTO pipeline_runs (run_type, trigger_source, started_at, status, created_at) "
        "VALUES (?, 'manual', ?, 'running', ?)",
        (pipeline_runs.RUN_TYPE, now, now),
    )
    conn.commit()
    assert pipeline_runs.is_running(conn) is True
    with pytest.raises(pipeline_runs.RunInProgressError):
        _run(conn)


def test_stale_running_run_is_reaped(conn, monkeypatch):
    _install_connectors(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az")})
    old = (datetime.now(timezone.utc)
           - timedelta(minutes=settings.MORNING_RUN_STALE_MINUTES + 30)).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO pipeline_runs (run_type, trigger_source, started_at, status, created_at) "
        "VALUES (?, 'manual', ?, 'running', ?)",
        (pipeline_runs.RUN_TYPE, old, old),
    )
    conn.commit()
    # A stale 'running' row must not block a fresh run.
    summary = _run(conn)
    assert summary["status"] in ("succeeded", "partial", "failed")
    reaped = conn.execute(
        "SELECT status FROM pipeline_runs WHERE started_at=?", (old,)).fetchone()
    assert reaped["status"] == "failed"


# --------------------------------------------------------------------------
# 6. Idempotent rerun.
# --------------------------------------------------------------------------

def test_idempotent_rerun(conn, monkeypatch):
    records = [_permit(permit_number="PHX-1", status="Applied", filed_date=_today_str(),
                       description="Warehouse")]
    _install_connectors(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az", records=records)})
    first = _run(conn)
    assert first["records_created"] == 1

    # Re-run with the identical record -> nothing new, everything unchanged.
    _install_connectors(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az", records=records)})
    second = _run(conn)
    assert second["records_created"] == 0
    assert second["records_updated"] == 0
    assert second["records_unchanged"] == 1
    # Still exactly one permit + one project.
    assert conn.execute("SELECT COUNT(*) AS n FROM permits").fetchone()["n"] == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"] == 1


# --------------------------------------------------------------------------
# 7. Updated lifecycle records (submitted -> issued) preserve history + re-score.
# --------------------------------------------------------------------------

def test_updated_lifecycle_records(conn, monkeypatch):
    _install_connectors(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az", records=[
        _permit(permit_number="PHX-1", status="Applied", filed_date="2026-07-01"),
    ])})
    _run(conn)
    lc1 = conn.execute("SELECT project_lifecycle FROM projects").fetchone()["project_lifecycle"]
    assert lc1 == "Application Submitted"

    # Same permit now issued.
    _install_connectors(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az", records=[
        _permit(permit_number="PHX-1", status="Issued", filed_date="2026-07-01",
                issued_date=_today_str()),
    ])})
    second = _run(conn)
    assert second["records_updated"] == 1
    row = conn.execute("SELECT status, issued_date FROM permits WHERE permit_number='PHX-1'").fetchone()
    assert row["status"] == "Issued"  # raw lifecycle history advanced
    lc2 = conn.execute("SELECT project_lifecycle FROM projects").fetchone()["project_lifecycle"]
    assert lc2 == "Permit Issued"


# --------------------------------------------------------------------------
# 8. Submitted-only record ingestion.
# --------------------------------------------------------------------------

def test_submitted_only_ingestion(conn, monkeypatch):
    _install_connectors(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az", records=[
        _permit(permit_number="PHX-1", status="Applied", filed_date=_today_str()),
        _permit(permit_number="PHX-2", status="Under Review", filed_date=_today_str()),
        _permit(permit_number="PHX-3", status="Issued", issued_date=_today_str()),
    ])})
    summary = _run(conn)
    # Two submitted-stage permits with no issued date.
    assert summary["submitted_only_records"] == 2
    row = conn.execute("SELECT submitted_only_records FROM pipeline_runs WHERE id=?",
                       (summary["run_id"],)).fetchone()
    assert row["submitted_only_records"] == 2


# --------------------------------------------------------------------------
# 9. Dashboard freshness status.
# --------------------------------------------------------------------------

def test_freshness_current_delayed_stale_failed(conn, monkeypatch):
    _install_connectors(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", records=[
            _permit(permit_number="PHX-1", status="Issued", issued_date=_today_str()),
        ]),
        "mesa_az": FakeConnector("mesa_az", records=[
            _permit(permit_number="MSA-1", status="Issued", issued_date="2020-01-01"),
        ]),
    })
    summary = _run(conn)
    fresh = {j["slug"]: j for j in summary["jurisdictions"]}
    assert fresh["phoenix_az"]["status"] == "Current"
    assert fresh["mesa_az"]["status"] == "Stale"

    # A failed source is flagged Failed regardless of any prior data.
    _install_connectors(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", fail_seq=[InvalidRequestError("x")]),
        "mesa_az": FakeConnector("mesa_az", records=[
            _permit(permit_number="MSA-1", status="Issued", issued_date="2020-01-01")]),
    })
    summary2 = _run(conn)
    fresh2 = {j["slug"]: j for j in summary2["jurisdictions"]}
    assert fresh2["phoenix_az"]["status"] == "Failed"


# --------------------------------------------------------------------------
# 11. Log and run-record + summary creation.
# --------------------------------------------------------------------------

def test_logs_and_summary_created(conn, monkeypatch, tmp_path):
    _install_connectors(monkeypatch, {"phoenix_az": FakeConnector("phoenix_az", records=[
        _permit(permit_number="PHX-1", status="Applied", filed_date=_today_str())])})
    summary = _run(conn)

    log_dir = settings.MORNING_REFRESH_LOG_DIR
    assert log_dir.exists() and any(log_dir.glob("morning_refresh_*.log"))
    md = settings.REPORTS_GENERATED_DIR / f"morning_refresh_summary_{pipeline_runs._today()}.md"
    assert md.exists()
    assert "Morning refresh" in md.read_text(encoding="utf-8")
    # Employee-facing status reads back the recorded run.
    emp = pipeline_runs.employee_status(conn)
    assert emp["status"] == summary["status"]
    assert emp["label"] in ("Data current", "Refresh partially delayed", "Data refresh failed")


# --------------------------------------------------------------------------
# 10. Admin-only manual trigger (HTTP integration).
# --------------------------------------------------------------------------

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    import pipeline.api.server as server_mod
    db_file = tmp_path / "mr_http.db"

    def factory():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        cc.execute("PRAGMA foreign_keys = ON")
        return cc

    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    # No 'connected' jurisdiction -> the background run does no network and
    # completes immediately; we only assert authz + that a run is recorded.
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) "
              "VALUES ('phoenix_az','Phoenix','AZ','pending')")
    seed_auth(c)
    org = c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    auth.create_user(c, organization_id=org, email="mradmin@corridoriq.com",
                     password="MrAdmin123", role_names=["admin"], must_change_password=False)
    auth.create_user(c, organization_id=org, email="mrrep@corridoriq.com",
                     password="MrRep123", role_names=["sales_representative"],
                     must_change_password=False)
    c.commit()
    c.close()

    monkeypatch.setattr(server_mod, "_factory", factory)
    monkeypatch.setattr(settings, "DATA_EXPORTS_DIR", tmp_path / "exports")
    monkeypatch.setattr(settings, "REPORTS_GENERATED_DIR", tmp_path / "reports")
    monkeypatch.setattr(settings, "MORNING_REFRESH_LOG_DIR", tmp_path / "logs")
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo-out"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    yield {"port": port, "db_file": db_file, "factory": factory}
    srv.shutdown()


def _req(port, method, path, body=None, cookie=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    c.request(method, path, json.dumps(body) if body is not None else None, headers)
    r = c.getresponse()
    raw = r.read()
    sc = r.getheader("Set-Cookie")
    c.close()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        data = raw
    return r.status, data, sc


def _login(port, email, pw):
    _, _, sc = _req(port, "POST", "/api/auth/login", {"email": email, "password": pw})
    return sc.split(";")[0]


def test_status_refresh_available_to_any_user(http_server):
    port = http_server["port"]
    cookie = _login(port, "mrrep@corridoriq.com", "MrRep123")
    status, data, _ = _req(port, "GET", "/api/status/refresh", cookie=cookie)
    assert status == 200
    assert "label" in data


def test_admin_detail_and_trigger_require_permission(http_server):
    port = http_server["port"]
    rep = _login(port, "mrrep@corridoriq.com", "MrRep123")
    # Rep cannot view detailed status or trigger a run.
    assert _req(port, "GET", "/api/admin/morning-refresh", cookie=rep)[0] == 403
    assert _req(port, "POST", "/api/admin/morning-refresh/run", {}, cookie=rep)[0] == 403


def test_admin_can_trigger_run(http_server):
    port = http_server["port"]
    admin = _login(port, "mradmin@corridoriq.com", "MrAdmin123")
    assert _req(port, "GET", "/api/admin/morning-refresh", cookie=admin)[0] == 200
    status, data, _ = _req(port, "POST", "/api/admin/morning-refresh/run", {}, cookie=admin)
    assert status == 202
    # The background run records a manual pipeline_runs row.
    factory = http_server["factory"]
    found = False
    for _ in range(40):
        cc = factory()
        row = cc.execute(
            "SELECT trigger_source, status FROM pipeline_runs "
            "WHERE run_type='morning_refresh' ORDER BY id DESC LIMIT 1").fetchone()
        cc.close()
        if row and row["trigger_source"] == "manual":
            found = True
            break
        time.sleep(0.1)
    assert found


def _success_connectors(monkeypatch):
    _install_connectors(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", records=[
            _permit(permit_number="PHX-1", status="Applied", filed_date=_today_str(),
                    description="New commercial building", permit_type="Building"),
        ]),
        "mesa_az": FakeConnector("mesa_az", records=[
            _permit(permit_number="MSA-1", status="Issued", issued_date=_today_str(),
                    description="Tenant improvement plumbing", permit_type="Plumbing"),
        ]),
    })


def test_successful_refresh_publishes_owner_brief(conn, monkeypatch, tmp_path):
    _success_connectors(monkeypatch)
    summary = _run(conn)
    assert summary["status"] == "succeeded"
    latest = tmp_path / "ceo-out" / "intelligence" / "latest_daily_brief.json"
    payload = json.loads(latest.read_text(encoding="utf-8"))
    assert payload["execute"] is False
    assert payload["provenance"]["refresh_run_id"] == summary["run_id"]
    assert payload["provenance"]["scope"] == {"kind": "owner", "organization_id": None}
    history = tmp_path / "ceo-out" / "intelligence" / "history"
    assert (history / f"refresh-{summary['run_id']:06d}.json").is_file()
    assert (history / f"refresh-{summary['run_id']:06d}.md").is_file()


def test_failed_and_partial_refresh_do_not_publish(conn, monkeypatch, tmp_path):
    _install_connectors(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", fail_seq=[InvalidRequestError("nope")]),
        "mesa_az": FakeConnector("mesa_az", fail_seq=[InvalidRequestError("nope")]),
    })
    failed = _run(conn)
    assert failed["status"] == "failed"
    assert not (tmp_path / "ceo-out" / "intelligence" / "latest_daily_brief.json").exists()

    _install_connectors(monkeypatch, {
        "phoenix_az": FakeConnector("phoenix_az", records=[
            _permit(permit_number="PHX-2", status="Applied", filed_date=_today_str()),
        ]),
        "mesa_az": FakeConnector("mesa_az", fail_seq=[InvalidRequestError("bad query")]),
    })
    partial = _run(conn)
    assert partial["status"] == "partial"
    assert not (tmp_path / "ceo-out" / "intelligence" / "latest_daily_brief.json").exists()


def test_brief_generation_failure_keeps_refresh_succeeded(conn, monkeypatch, tmp_path):
    _success_connectors(monkeypatch)
    first = _run(conn)
    assert first["status"] == "succeeded"
    latest = tmp_path / "ceo-out" / "intelligence" / "latest_daily_brief.json"
    original = json.loads(latest.read_text(encoding="utf-8"))

    def _boom(**kwargs):
        raise RuntimeError("brief generator failed")

    monkeypatch.setattr("agents.ceo.analytics.publish.build_daily_brief", _boom)
    second = _run(conn)
    assert second["status"] == "succeeded"
    stored = conn.execute(
        "SELECT status FROM pipeline_runs WHERE id=?", (second["run_id"],)
    ).fetchone()
    assert stored["status"] == "succeeded"
    again = json.loads(latest.read_text(encoding="utf-8"))
    assert again["provenance"]["refresh_run_id"] == original["provenance"]["refresh_run_id"] == first["run_id"]
    status = json.loads((tmp_path / "ceo-out" / "intelligence" / "latest_brief_status.json").read_text(encoding="utf-8"))
    assert status["latest_attempt"]["brief_status"] == "failed"
    assert status["latest_attempt"]["refresh_run_id"] == second["run_id"]
    assert status["latest_valid"]["refresh_run_id"] == first["run_id"]
