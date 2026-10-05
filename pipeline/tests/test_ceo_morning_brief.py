"""Owner morning brief: server authorization, freshness, and read-only publishing."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agents.ceo.analytics.answer import answer_question
from agents.ceo.analytics.guard import open_analytics
from agents.ceo.analytics.publish import publish_owner_brief
from agents.ceo.analytics.serve import MALFORMED_MESSAGE, MISSING_MESSAGE, load_owner_brief_view
from agents.ceo.tests.test_data_intelligence import AS_OF, _build
from pipeline.api.server import static_target


ROOT = Path(__file__).resolve().parents[2]


def _display():
    from agents.ceo.analytics.brief import DISPLAY_SECTIONS
    return [
        {"id": key, "heading": heading, "claim_class": claim, "text": f"{heading} text"}
        for key, heading, claim in DISPLAY_SECTIONS
    ]


def _brief(*, run_id=1, organization_id=None, execute=False, status="KNOWN", text=None):
    display = _display()
    if text is not None:
        display[0]["text"] = text
    return {
        "schema": "corridoriq.ceo.daily_brief.v1",
        "generated_at": "2026-10-05T12:00:00Z",
        "as_of": "2026-10-05",
        "execute": execute,
        "status": status,
        "display": display,
        "sections": {"freshness": {"rows": [{"jurisdiction_slug": "chandler_az", "stale": True}]}},
        "provenance": {
            "generated_at": "2026-10-05T12:00:00Z",
            "data_as_of": "2026-10-05",
            "latest_successful_refresh": "2026-10-05T03:01:00",
            "refresh_run_id": run_id,
            "refresh_completed_at": "2026-10-05T03:01:00",
            "scope": {"kind": "owner", "organization_id": organization_id},
            "generation_status": "succeeded",
            "schema": "corridoriq.ceo.daily_brief.v1",
        },
    }


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE pipeline_runs (id INTEGER PRIMARY KEY, run_type TEXT, status TEXT, completed_at TEXT)"
    )
    return conn


def _write(directory: Path, payload) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "latest_daily_brief.json").write_text(json.dumps(payload), encoding="utf-8")


def test_missing_brief_is_explicit(tmp_path):
    view = load_owner_brief_view(reports_conn=_conn(), directory=tmp_path)
    assert view["available"] is False
    assert view["execute"] is False
    assert view["message"] == MISSING_MESSAGE
    assert view["sections"] == []


def test_valid_brief_is_current_when_it_matches_the_refresh(tmp_path):
    _write(tmp_path, _brief(run_id=4))
    conn = _conn()
    conn.execute(
        "INSERT INTO pipeline_runs (id, run_type, status, completed_at) VALUES (4, 'morning_refresh', 'succeeded', ?)",
        ("2026-10-05T03:01:00",),
    )
    view = load_owner_brief_view(reports_conn=conn, directory=tmp_path)
    assert view["available"] is True
    assert view["current"] is True
    assert view["execute"] is False
    assert view["notice"] is None
    assert view["provenance"]["data_as_of"] == "2026-10-05"
    assert view["provenance"]["refresh_run_id"] == 4
    assert view["provenance"]["latest_successful_refresh"] == "2026-10-05T03:01:00"
    assert view["provenance"]["generated_at"] == "2026-10-05T12:00:00Z"
    assert view["feeds_stale"] == ["chandler_az"]
    assert {item["heading"] for item in view["sections"]} >= {
        "EXECUTIVE SUMMARY", "WHAT CHANGED", "TOP OPPORTUNITIES", "WHY NOW",
        "MARKET MOVEMENT", "CUSTOMER / SUPPLIER SIGNALS", "CONTACT GAPS",
        "PIPELINE & DATA HEALTH", "RISKS / UNCERTAINTIES", "CEO PRIORITIES TODAY",
    }


def test_malformed_brief_is_not_shown(tmp_path):
    (tmp_path / "latest_daily_brief.json").write_text("{", encoding="utf-8")
    view = load_owner_brief_view(reports_conn=_conn(), directory=tmp_path)
    assert view["available"] is False
    assert view["message"] == MALFORMED_MESSAGE
    assert "latest_daily_brief" not in json.dumps(view)


def test_older_brief_stays_distinct_from_a_failed_newer_refresh(tmp_path):
    _write(tmp_path, _brief(run_id=1))
    conn = _conn()
    conn.execute(
        "INSERT INTO pipeline_runs (id, run_type, status, completed_at) VALUES (1, 'morning_refresh', 'succeeded', '2026-10-05T03:01:00')"
    )
    conn.execute(
        "INSERT INTO pipeline_runs (id, run_type, status, completed_at) VALUES (2, 'morning_refresh', 'failed', '2026-10-06T03:01:00')"
    )
    view = load_owner_brief_view(reports_conn=conn, directory=tmp_path)
    assert view["available"] is True
    assert view["current"] is False
    assert view["provenance"]["refresh_run_id"] == 1
    assert "2026-10-06T03:01:00" in view["notice"]
    assert "failed" in view["notice"]
    assert "was not generated from this refresh" in view["notice"]


def test_brief_failure_after_success_keeps_the_previous_brief_identifiable(tmp_path, monkeypatch):
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo"))
    db = tmp_path / "intel.db"
    _build(db)
    # The fixture's succeeded run is not this id; publishing still stamps the caller.
    first = publish_owner_brief(db_path=db, refresh={
        "run_id": 7, "status": "succeeded", "completed_at": "2026-10-05T03:01:00",
    })
    assert first["ok"] is True
    failed = publish_owner_brief(db_path=db, refresh={
        "run_id": 8, "status": "succeeded", "completed_at": "2026-10-06T03:01:00",
    })
    # Force the failure path on a second call by breaking generation after the first success.
    assert failed["ok"] is True

    def _boom(**kwargs):
        raise RuntimeError("generator failed")

    monkeypatch.setattr("agents.ceo.analytics.publish.build_daily_brief", _boom)
    third = publish_owner_brief(db_path=db, refresh={
        "run_id": 9, "status": "succeeded", "completed_at": "2026-10-07T03:01:00",
    })
    assert third["brief_status"] == "failed"
    latest = json.loads((tmp_path / "ceo" / "intelligence" / "latest_daily_brief.json").read_text(encoding="utf-8"))
    assert latest["provenance"]["refresh_run_id"] == 8
    assert latest["execute"] is False
    status = json.loads((tmp_path / "ceo" / "intelligence" / "latest_brief_status.json").read_text(encoding="utf-8"))
    assert status["latest_attempt"]["refresh_status"] == "succeeded"
    assert status["latest_attempt"]["brief_status"] == "failed"
    assert status["latest_valid"]["refresh_run_id"] == 8
    conn = _conn()
    conn.execute(
        "INSERT INTO pipeline_runs (id, run_type, status, completed_at) VALUES (9, 'morning_refresh', 'succeeded', '2026-10-07T03:01:00')"
    )
    view = load_owner_brief_view(reports_conn=conn, directory=tmp_path / "ceo" / "intelligence")
    assert view["current"] is False
    assert view["provenance"]["refresh_run_id"] == 8
    assert "2026-10-07T03:01:00" in view["notice"]
    assert view["latest_attempt"]["brief_status"] == "failed"


def test_failed_refresh_publish_does_not_create_a_brief(tmp_path, monkeypatch):
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo"))
    db = tmp_path / "intel.db"
    _build(db)
    result = publish_owner_brief(db_path=db, refresh={
        "run_id": 3, "status": "failed", "completed_at": "2026-10-06T03:01:00",
    })
    assert result["brief_status"] == "skipped"
    assert not (tmp_path / "ceo" / "intelligence" / "latest_daily_brief.json").exists()


def test_organization_scoped_file_is_rejected(tmp_path):
    _write(tmp_path, _brief(organization_id=2, text="ORG2-SECRET"))
    view = load_owner_brief_view(reports_conn=_conn(), directory=tmp_path)
    assert view["available"] is False
    assert "ORG2-SECRET" not in json.dumps(view)


def test_customer_book_still_requires_an_organization(tmp_path):
    db = tmp_path / "intel.db"
    _build(db)
    missing = answer_question(
        "Which supplier opportunities deserve immediate review?",
        db_path=db, as_of=AS_OF,
    )
    assert missing["status"] == "UNKNOWN"
    assert "tenant unresolved" in missing["claims"][0]["text"]
    unsupported = answer_question("How many dollars of inventory should we quote?", db_path=db, as_of=AS_OF)
    assert unsupported["status"] == "UNSUPPORTED"
    assert unsupported["execute"] is False


def test_analytics_update_remains_denied(tmp_path):
    db = tmp_path / "ro.db"
    raw = sqlite3.connect(db)
    raw.execute("CREATE TABLE companies (display_name TEXT)")
    raw.execute("INSERT INTO companies (display_name) VALUES ('Alpha')")
    raw.commit()
    raw.close()
    conn = open_analytics(db)
    try:
        with pytest.raises(sqlite3.Error):
            conn.execute("UPDATE companies SET display_name = 'x'")
        assert conn.execute("SELECT display_name FROM companies").fetchone()["display_name"] == "Alpha"
    finally:
        conn.close()


def test_navigation_is_only_in_the_admin_menu():
    text = (ROOT / "portal-common.js").read_text(encoding="utf-8")
    admin = text.split("if (isAdmin)")[1].split("if (isManager)")[0]
    manager = text.split("if (isManager)")[1].split("if (isEstimator)")[0]
    assert "ceo-morning-brief.html" in admin
    assert "ceo-morning-brief.html" not in manager
    page = (ROOT / "ceo-morning-brief.js").read_text(encoding="utf-8")
    assert 'CIQ.guard("admin.system"' in page
    assert static_target("/ceo-morning-brief.html")[0] is not None
    assert static_target("/ceo-morning-brief.js")[0] is not None


# --- HTTP authorization ----------------------------------------------------

import http.client
import threading
import time
from http.server import ThreadingHTTPServer

from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.config import settings
from pipeline.config.settings import SCHEMA_PATH


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    import pipeline.api.server as server_mod
    db_file = tmp_path / "brief_http.db"

    def factory():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        cc.execute("PRAGMA foreign_keys = ON")
        return cc

    conn = factory()
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(conn)
    org = conn.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    users = (
        ("owner@corridoriq.com", "OwnerPass123", ["admin"]),
        ("rep@corridoriq.com", "RepPass123", ["sales_representative"]),
        ("manager@corridoriq.com", "MgrPass123", ["sales_manager"]),
        ("reader@corridoriq.com", "ReadPass123", ["read_only"]),
        ("fulfill@corridoriq.com", "FulPass123", ["fulfillment_user"]),
    )
    for email, password, roles in users:
        auth.create_user(conn, organization_id=org, email=email, password=password,
                         role_names=roles, must_change_password=False)
    conn.commit()
    conn.close()
    monkeypatch.setattr(server_mod, "_factory", factory)
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo-out"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield {"port": port, "db": db_file}
    srv.shutdown()


def _req(port, method, path, body=None, cookie=None):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    client.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = client.getresponse()
    raw = response.read()
    set_cookie = response.getheader("Set-Cookie")
    client.close()
    try:
        data = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        data = raw
    return response.status, data, set_cookie


def _login(port, email, password):
    status, _data, cookie = _req(port, "POST", "/api/auth/login", {"email": email, "password": password})
    assert status == 200
    return cookie.split(";")[0]


def test_owner_can_read_the_brief_and_other_roles_cannot(http_server, tmp_path):
    port = http_server["port"]
    directory = tmp_path / "ceo-out" / "intelligence"
    _write(directory, _brief(run_id=1, text="Owner market brief"))
    conn = sqlite3.connect(http_server["db"])
    conn.execute(
        """
        INSERT INTO pipeline_runs (run_type, status, completed_at, started_at, created_at)
        VALUES ('morning_refresh', 'succeeded', ?, ?, ?)
        """,
        ("2026-10-05T03:01:00", "2026-10-05T03:00:00", "2026-10-05T03:00:00"),
    )
    conn.commit()
    run_id = conn.execute("SELECT id FROM pipeline_runs").fetchone()[0]
    conn.close()
    # Align the fixture with the inserted row.
    _write(directory, _brief(run_id=run_id, text="Owner market brief"))

    assert _req(port, "GET", "/api/admin/ceo-morning-brief")[0] == 401
    assert _req(port, "GET", "/api/admin/ceo-morning-brief", cookie="corridoriq_pilot_session=contractor-token")[0] == 401
    for email, password in (
        ("rep@corridoriq.com", "RepPass123"),
        ("manager@corridoriq.com", "MgrPass123"),
        ("reader@corridoriq.com", "ReadPass123"),
        ("fulfill@corridoriq.com", "FulPass123"),
    ):
        cookie = _login(port, email, password)
        status, body, _ = _req(port, "GET", "/api/admin/ceo-morning-brief", cookie=cookie)
        assert status == 403, email
        assert "Owner market brief" not in json.dumps(body)

    owner = _login(port, "owner@corridoriq.com", "OwnerPass123")
    status, body, _ = _req(port, "GET", "/api/admin/ceo-morning-brief?organization=2", cookie=owner)
    assert status == 200
    assert body["available"] is True
    assert body["execute"] is False
    assert body["provenance"]["scope"]["organization_id"] is None
    assert "ORG2-SECRET" not in json.dumps(body)
    assert any(item["text"] == "Owner market brief" for item in body["sections"])
