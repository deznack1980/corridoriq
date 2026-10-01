"""Sprint 6 (Sales Workspace) tests — dashboard, company lists, opportunities,
activity feed, follow-ups, manager workload, reports catalog, and the
record-level/permission regressions that back the redesigned UI.

These exercise the backend (where authorization is enforced). Hiding a nav link
is never trusted; every assertion here confirms the server enforces access.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from pipeline.auth import service as auth
from pipeline.auth.rbac import AuthzError
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import SCHEMA_PATH
from pipeline.crm import admin as crm_admin
from pipeline.crm import service as crm
from pipeline.reports import catalog as reports_catalog


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) "
              "VALUES ('phoenix_az','Phoenix','AZ','connected')")
    seed_auth(c)
    return c


def _org(c):
    return c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]


def _ctx(c, uid):
    return auth.build_user_context(c, c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def _company(c, name, city="PHOENIX"):
    now = _now()
    cur = c.execute("INSERT INTO companies (normalized_name, display_name, city, state, "
                    "lifecycle_state, created_at, updated_at) VALUES (?,?,?,?,'active',?,?)",
                    (name.upper(), name, city, "AZ", now, now))
    c.commit()
    return cur.lastrowid


def _intel(c, company_id, tier="High", score=75, **extra):
    cols = {"company_id": company_id, "company_priority_tier": tier,
            "company_priority_score": score, "active_projects": 2,
            "projects_last_30_days": 1, "total_projects": 3,
            "average_opportunity_score": 70, "highest_opportunity_score": 88,
            "municipality_count": 1, "latest_activity_date": _today(),
            "model_version": "test"}
    cols.update(extra)
    keys = ", ".join(cols)
    qs = ", ".join("?" for _ in cols)
    c.execute(f"INSERT INTO company_intelligence ({keys}) VALUES ({qs})", list(cols.values()))
    c.commit()


def _project(c, company_id, score=90, lifecycle="permitting", days_ago=1):
    now = _now()
    d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    cur = c.execute("INSERT INTO permits (jurisdiction, permit_number, job_address, city, "
                    "state, status, contractor_company_id, issued_date, first_seen_at, last_updated_at) "
                    "VALUES ('phoenix_az',?,?, 'PHOENIX','AZ','issued',?,?,?,?)",
                    (f"P{company_id}-{days_ago}", "123 Main St", company_id, d, now, now))
    pid = cur.lastrowid
    c.execute("INSERT INTO projects (permit_id, jurisdiction, contractor_company_id, "
              "project_category, project_lifecycle, opportunity_score, opportunity_date, "
              "opportunity_timing, estimated_material_value) "
              "VALUES (?, 'phoenix_az', ?, 'commercial', ?, ?, ?, 'immediate', 5000)",
              (pid, company_id, lifecycle, score, d))
    c.commit()


@pytest.fixture()
def env(conn):
    org = _org(conn)
    admin = auth.create_user(conn, organization_id=org, email="admin@corridoriq.com",
                             password="AdminPass123", role_names=["admin"], must_change_password=False)
    mgr = auth.create_user(conn, organization_id=org, email="mgr@corridoriq.com",
                           password="MgrPass123", role_names=["sales_manager"], must_change_password=False)
    rep = auth.create_user(conn, organization_id=org, email="rep@corridoriq.com",
                           password="RepPass123", role_names=["sales_representative"], must_change_password=False)
    rep2 = auth.create_user(conn, organization_id=org, email="rep2@corridoriq.com",
                            password="Rep2Pass123", role_names=["sales_representative"], must_change_password=False)
    co1 = _company(conn, "Alpha Plumbing")
    co2 = _company(conn, "Beta Builders")
    _intel(conn, co1, "Critical", 95)
    _intel(conn, co2, "Medium", 50)
    e = {"conn": conn, "org": org, "co1": co1, "co2": co2,
         "admin": _ctx(conn, admin), "mgr": _ctx(conn, mgr),
         "rep": _ctx(conn, rep), "rep2": _ctx(conn, rep2),
         "ids": {"admin": admin, "mgr": mgr, "rep": rep, "rep2": rep2}}
    # Assign co1 -> rep, co2 -> rep2.
    crm.assign_company(conn, e["admin"], co1, rep)
    crm.assign_company(conn, e["admin"], co2, rep2)
    return e


# --------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------

def test_dashboard_shape_and_counts(env):
    c = env["conn"]
    d = crm.dashboard(c, env["rep"])
    assert set(["kpis", "priority_companies", "followups_due",
                "recent_opportunity_activity", "activity_summary"]).issubset(d)
    # Record-level: rep sees only their one assigned company.
    assert d["kpis"]["my_companies"] == 1
    assert d["my_companies"] == 1  # back-compat key retained
    assert len(d["priority_companies"]) == 1
    assert d["priority_companies"][0]["company_id"] == env["co1"]
    for k in ("calls_logged", "companies_contacted"):
        assert k in d["activity_summary"]


def test_dashboard_overdue_and_calls_due(env):
    c = env["conn"]
    # Follow-up today for co1 (calls due today) and an overdue one via direct update.
    crm.create_activity(c, env["rep"], env["co1"],
                        {"activity_type": "call", "next_followup_at": _today() + "T09:00:00"})
    d = crm.dashboard(c, env["rep"])
    assert d["kpis"]["calls_due_today"] == 1
    past = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d")
    c.execute("UPDATE crm_company_relationships SET next_followup_at=? WHERE company_id=?",
              (past + "T09:00:00", env["co1"]))
    c.commit()
    d2 = crm.dashboard(c, env["rep"])
    assert d2["kpis"]["overdue_followups"] == 1


def test_manager_dashboard_sees_all_org(env):
    d = crm.dashboard(env["conn"], env["mgr"])
    assert d["kpis"]["my_companies"] == 2  # manager scope = whole org
    assert d["assignment_scoped"] is False
    assert d["scope"] == "organization"


def test_rep_dashboard_is_assignment_scoped(env):
    d = crm.dashboard(env["conn"], env["rep"])
    assert d["assignment_scoped"] is True
    assert d["scope"] == "assignment"
    assert d["kpis"]["my_companies"] == 1


# --------------------------------------------------------------------------
# Role-aware landing + admin organization dashboard
# --------------------------------------------------------------------------

def test_default_landing_page_by_role(env):
    from pipeline.auth.rbac import default_landing_page

    assert env["admin"]["default_landing_page"] == "admin-dashboard.html"
    assert env["mgr"]["default_landing_page"] == "team-dashboard.html"
    assert env["rep"]["default_landing_page"] == "sales-dashboard.html"
    assert default_landing_page(["estimator"], {"projects.view_assigned"}) == "estimator-work-queue.html"
    assert default_landing_page(["read_only"], {"companies.view_assigned"}) == "readonly-dashboard.html"


def test_admin_dashboard_is_organization_scoped_not_assignment(env):
    c = env["conn"]
    # Unassigned company should still count in org totals.
    orphan = _company(c, "Orphan Mechanical")
    _intel(c, orphan, "High", 80)
    _project(c, env["co1"], score=85, lifecycle="Permit Issued")

    dash = crm_admin.admin_dashboard(c, env["admin"])
    assert dash["scope"] == "organization"
    assert dash["assignment_scoped"] is False
    k = dash["kpis"]
    assert k["total_companies"] >= 3
    assert k["total_projects"] >= 1
    assert k["total_permits"] >= 1
    assert k["companies_awaiting_assignment"] >= 1
    assert k["active_employees"] >= 4
    # Must never look like a personal assignment dashboard.
    assert "my_companies" not in k
    assert "No companies assigned to you" not in json.dumps(dash)


def test_rep_cannot_access_admin_dashboard(env):
    with pytest.raises(AuthzError):
        crm_admin.admin_dashboard(env["conn"], env["rep"])


def test_estimator_work_queue_accessible(env):
    c = env["conn"]
    org = env["org"]
    est_id = auth.create_user(c, organization_id=org, email="est@corridoriq.com",
                              password="EstPass123", role_names=["estimator"],
                              must_change_password=False)
    est = _ctx(c, est_id)
    assert est["default_landing_page"] == "estimator-work-queue.html"
    _project(c, env["co1"], score=90, lifecycle="Permit Issued")
    q = crm_admin.estimator_work_queue(c, est)
    assert q["scope"] == "estimator"
    assert q["assignment_scoped"] is True
    assert isinstance(q["queue"], list)
def test_list_reason_and_recommended_action(env):
    res = crm.list_my_companies(env["conn"], env["rep"])
    item = res["items"][0]
    # The reason comes from account relevance (trust layer), not the legacy tier.
    assert "Not yet assessed" in item["reason"]
    assert "Critical" not in item["reason"]
    assert item["account_relevance"]["status"] == "NOT_ASSESSED"
    assert item["recommended_action"] == "Make first contact"  # never contacted
    assert item["followup_overdue"] is False


def test_list_pagination(env):
    res = crm.list_my_companies(env["conn"], env["mgr"], {"page_size": 1})
    assert res["total"] == 2 and res["pages"] == 2 and len(res["items"]) == 1
    res2 = crm.list_my_companies(env["conn"], env["mgr"], {"page_size": 1, "page": 2})
    assert res2["items"][0]["company_id"] != res["items"][0]["company_id"]


def test_list_record_level_scoping(env):
    res = crm.list_my_companies(env["conn"], env["rep"])
    assert res["total"] == 1 and res["items"][0]["company_id"] == env["co1"]


def test_list_followup_filter(env):
    c = env["conn"]
    crm.create_activity(c, env["rep"], env["co1"],
                        {"activity_type": "call", "next_followup_at": _today() + "T10:00:00"})
    due = crm.list_my_companies(c, env["rep"], {"followup": "due"})
    assert due["total"] == 1
    none_due = crm.list_my_companies(c, env["rep2"], {"followup": "due"})
    assert none_due["total"] == 0


def test_list_never_contacted_filter(env):
    res = crm.list_my_companies(env["conn"], env["rep"], {"contacted": "never"})
    assert res["total"] == 1
    crm.create_activity(env["conn"], env["rep"], env["co1"], {"activity_type": "call"})
    res2 = crm.list_my_companies(env["conn"], env["rep"], {"contacted": "never"})
    assert res2["total"] == 0


# --------------------------------------------------------------------------
# Follow-ups due
# --------------------------------------------------------------------------

def test_followups_due(env):
    c = env["conn"]
    crm.create_activity(c, env["rep"], env["co1"],
                        {"activity_type": "call", "activity_outcome": "left_voicemail",
                         "next_followup_at": _today() + "T10:00:00"})
    items = crm.followups_due(c, env["rep"])
    assert len(items) == 1
    assert items[0]["company_id"] == env["co1"]
    assert items[0]["last_outcome"] == "left_voicemail"
    assert "recommended_action" in items[0]


def test_followups_due_scoped(env):
    c = env["conn"]
    crm.create_activity(c, env["rep"], env["co1"],
                        {"activity_type": "call", "next_followup_at": _today() + "T10:00:00"})
    assert crm.followups_due(c, env["rep2"]) == []


# --------------------------------------------------------------------------
# Opportunities
# --------------------------------------------------------------------------

def test_opportunities_record_level(env):
    c = env["conn"]
    _project(c, env["co1"], score=90)
    _project(c, env["co2"], score=95)
    rep_view = crm.opportunities(c, env["rep"])
    assert rep_view["total"] == 1
    assert rep_view["items"][0]["company_id"] == env["co1"]
    # Manager sees both.
    assert crm.opportunities(c, env["mgr"])["total"] == 2


def test_opportunities_score_filter(env):
    c = env["conn"]
    _project(c, env["co1"], score=90)
    _project(c, env["co1"], score=30, days_ago=2)
    res = crm.opportunities(c, env["rep"], {"score_min": 50})
    assert res["total"] == 1 and res["items"][0]["opportunity_score"] == 90


# --------------------------------------------------------------------------
# Personal activity feed
# --------------------------------------------------------------------------

def test_my_activity_is_author_scoped(env):
    c = env["conn"]
    crm.create_activity(c, env["rep"], env["co1"], {"activity_type": "note", "notes": "hi"})
    mine = crm.my_activity(c, env["rep"])
    assert mine["total"] == 1 and mine["items"][0]["display_name"] == "Alpha Plumbing"
    assert crm.my_activity(c, env["rep2"])["total"] == 0


# --------------------------------------------------------------------------
# Manager workload
# --------------------------------------------------------------------------

def test_team_overview_fields_and_permission(env):
    c = env["conn"]
    crm.create_activity(c, env["rep"], env["co1"],
                        {"activity_type": "call", "activity_outcome": "appointment_set"})
    team = crm_admin.team_overview(c, env["mgr"])
    rep_row = next(r for r in team if r["user_id"] == env["ids"]["rep"])
    for k in ("assigned_companies", "calls_completed", "appointments",
              "overdue_tasks", "followups_due", "companies_no_activity", "quote_requests"):
        assert k in rep_row
    assert rep_row["assigned_companies"] == 1
    assert rep_row["calls_completed"] == 1
    assert rep_row["appointments"] == 1
    # Reps cannot view team workload.
    with pytest.raises(AuthzError):
        crm_admin.team_overview(c, env["rep"])


# --------------------------------------------------------------------------
# Reports catalog + safe download
# --------------------------------------------------------------------------

def test_reports_catalog_permission(env):
    cat = reports_catalog.catalog(env["conn"], env["rep"])
    assert "reports" in cat and isinstance(cat["files"], list)
    assert any(r["key"] == "company_opportunity" for r in cat["reports"])


def test_safe_generated_path_blocks_traversal(tmp_path, monkeypatch):
    monkeypatch.setattr(reports_catalog.settings, "REPORTS_GENERATED_DIR", tmp_path)
    good = tmp_path / "company_opportunity.md"
    good.write_text("ok", encoding="utf-8")
    assert reports_catalog.safe_generated_path("company_opportunity.md") == good.resolve()
    assert reports_catalog.safe_generated_path("../secret.md") is None
    assert reports_catalog.safe_generated_path("nope.exe") is None
    assert reports_catalog.safe_generated_path("missing.md") is None


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------

def test_recommended_action_matrix():
    ra = crm.recommended_action
    assert ra("assigned", None, None) == "Make first contact"
    assert ra("won", None, "2026-01-01") == "Maintain relationship"
    assert ra("lost", None, "2026-01-01") == "Closed — lost"
    assert ra("contacted", None, None, do_not_contact=1) == "Do not contact"
    assert ra("quote_requested", None, "2026-01-01") == "Send quote"
    assert ra("contacted", "1999-01-01T00:00:00", "2026-01-01") == "Follow up now (due)"


# --------------------------------------------------------------------------
# Security regression — redesigned UI cannot reach unassigned data
# --------------------------------------------------------------------------

def test_rep_cannot_open_unassigned_company(env):
    with pytest.raises(AuthzError):
        crm.get_company_detail(env["conn"], env["rep"], env["co2"])


def test_rep_cannot_see_unassigned_opportunities(env):
    c = env["conn"]
    _project(c, env["co2"], score=99)
    res = crm.opportunities(c, env["rep"])
    assert all(i["company_id"] != env["co2"] for i in res["items"])


# --------------------------------------------------------------------------
# HTTP integration — routes, static pages, and enforcement are server-side
# --------------------------------------------------------------------------

import http.client
import threading
import time
from http.server import ThreadingHTTPServer


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    import pipeline.api.server as server_mod
    db_file = tmp_path / "s6_ui.db"

    def factory():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        cc.execute("PRAGMA foreign_keys = ON")
        return cc

    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) "
              "VALUES ('phoenix_az','Phoenix','AZ','connected')")
    seed_auth(c)
    org = _org(c)
    auth.create_user(c, organization_id=org, email="uiadmin@corridoriq.com",
                     password="UiAdmin123", role_names=["admin"], must_change_password=False)
    rep_id = auth.create_user(c, organization_id=org, email="uirep@corridoriq.com",
                              password="UiRep123", role_names=["sales_representative"],
                              must_change_password=False)
    c.commit()
    c.close()

    monkeypatch.setattr(server_mod, "_factory", factory)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    yield {"port": port, "rep_id": rep_id}
    srv.shutdown()


def _req(port, method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    resp = conn.getresponse()
    raw = resp.read()
    sc = resp.getheader("Set-Cookie")
    ct = resp.getheader("Content-Type")
    conn.close()
    try:
        data = json.loads(raw) if raw and ct and "json" in ct else raw
    except json.JSONDecodeError:
        data = raw
    return resp.status, data, sc


def _login(port, email, password):
    _, _, sc = _req(port, "POST", "/api/auth/login", {"email": email, "password": password})
    return sc.split(";")[0]


def test_http_new_routes_require_auth(http_server):
    for path in ("/api/sales/opportunities", "/api/sales/activity",
                 "/api/sales/followups", "/api/reports/catalog"):
        status, _, _ = _req(http_server["port"], "GET", path)
        assert status == 401, path


def test_http_workspace_endpoints_ok(http_server):
    port = http_server["port"]
    cookie = _login(port, "uiadmin@corridoriq.com", "UiAdmin123")
    for path in ("/api/sales/dashboard", "/api/sales/opportunities",
                 "/api/sales/activity", "/api/sales/followups",
                 "/api/manager/team", "/api/reports/catalog"):
        status, data, _ = _req(port, "GET", path, cookie=cookie)
        assert status == 200, path
        assert isinstance(data, dict)


def test_http_me_permissions_are_json_array(http_server):
    port = http_server["port"]
    cookie = _login(port, "uiadmin@corridoriq.com", "UiAdmin123")
    status, data, _ = _req(port, "GET", "/api/auth/me", cookie=cookie)
    assert status == 200
    # Nav gating depends on permissions being a real array, not a stringified set.
    assert isinstance(data["user"]["permissions"], list)
    assert "admin.system" in data["user"]["permissions"]
    assert isinstance(data["user"]["roles"], list)
    assert data["user"]["default_landing_page"] == "admin-dashboard.html"
    assert data["user"]["dashboard_mode"] == "organization"
    assert data["user"]["organization"]["name"]


def test_http_login_returns_role_landing_page(http_server):
    port = http_server["port"]
    status, data, sc = _req(port, "POST", "/api/auth/login",
                            {"email": "uiadmin@corridoriq.com", "password": "UiAdmin123"})
    assert status == 200
    assert data["user"]["default_landing_page"] == "admin-dashboard.html"
    assert sc and "HttpOnly" in sc

    status2, data2, _ = _req(port, "POST", "/api/auth/login",
                             {"email": "uirep@corridoriq.com", "password": "UiRep123"})
    assert status2 == 200
    assert data2["user"]["default_landing_page"] == "sales-dashboard.html"


def test_http_admin_dashboard_org_wide_not_assignment(http_server):
    port = http_server["port"]
    admin_cookie = _login(port, "uiadmin@corridoriq.com", "UiAdmin123")
    status, data, _ = _req(port, "GET", "/api/admin/dashboard", cookie=admin_cookie)
    assert status == 200
    assert data["assignment_scoped"] is False
    assert data["scope"] == "organization"
    assert "total_companies" in data["kpis"]
    assert "total_projects" in data["kpis"]
    assert "my_companies" not in data["kpis"]

    rep_cookie = _login(port, "uirep@corridoriq.com", "UiRep123")
    status2, _, _ = _req(port, "GET", "/api/admin/dashboard", cookie=rep_cookie)
    assert status2 == 403

    # Rep sales dashboard remains assignment-limited.
    status3, sales, _ = _req(port, "GET", "/api/sales/dashboard", cookie=rep_cookie)
    assert status3 == 200
    assert sales["assignment_scoped"] is True


def test_http_static_pages_served(http_server):
    port = http_server["port"]
    for name in ("sales-dashboard.html", "admin-dashboard.html", "team-dashboard.html",
                 "estimator-work-queue.html", "readonly-dashboard.html",
                 "my-companies.html", "opportunities.html",
                 "activity.html", "reports.html", "portal.css", "portal-common.js"):
        status, _, _ = _req(port, "GET", "/" + name)
        assert status == 200, name


def test_http_reports_download_blocks_traversal(http_server):
    port = http_server["port"]
    cookie = _login(port, "uiadmin@corridoriq.com", "UiAdmin123")
    status, _, _ = _req(port, "GET", "/api/reports/download?name=../server.py", cookie=cookie)
    assert status == 404
    status2, _, _ = _req(port, "GET", "/api/reports/download?name=nope.md", cookie=cookie)
    assert status2 == 404


def test_http_rep_denied_admin_and_manager_routes(http_server):
    port = http_server["port"]
    cookie = _login(port, "uirep@corridoriq.com", "UiRep123")
    for path in ("/api/admin/users", "/api/manager/team", "/api/manager/assignments"):
        status, _, _ = _req(port, "GET", path, cookie=cookie)
        assert status == 403, path


# --------------------------------------------------------------------------
# Login regression — POST route, JSON responses, cookie, no "Unsupported POST"
# --------------------------------------------------------------------------

def test_login_route_accepts_post_and_returns_json(http_server):
    port = http_server["port"]
    # Valid login -> 200 JSON + session cookie (never an "Unsupported" 501).
    status, data, sc = _req(port, "POST", "/api/auth/login",
                            {"email": "uiadmin@corridoriq.com", "password": "UiAdmin123"})
    assert status == 200
    assert isinstance(data, dict) and data.get("user", {}).get("email") == "uiadmin@corridoriq.com"
    assert sc and "HttpOnly" in sc  # session cookie created
    # Invalid login -> JSON error, not a page reload / not a 501.
    status2, data2, _ = _req(port, "POST", "/api/auth/login",
                             {"email": "uiadmin@corridoriq.com", "password": "wrong"})
    assert status2 == 401
    assert isinstance(data2, dict) and "error" in data2
    assert "Unsupported" not in json.dumps(data2)


def test_login_html_never_native_submits(http_server):
    """The login form must POST via fetch, never natively to login.html."""
    port = http_server["port"]
    status, body, _ = _req(port, "GET", "/login.html")
    assert status == 200
    html = body.decode("utf-8") if isinstance(body, (bytes, bytearray)) else str(body)
    assert 'onsubmit="return false"' in html
    assert "/api/auth/login" in html
    assert 'credentials: "same-origin"' in html
    # The form must not POST straight to a page.
    assert 'action="login.html"' not in html


def test_login_form_browser_flow(http_server):
    """End-to-end: submitting the form logs in, sets a cookie, and redirects;
    bad credentials show an inline error without leaving the page or hitting
    an 'Unsupported POST' error."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    import pipeline.api.server as server_mod

    base = f"http://127.0.0.1:{http_server['port']}"
    try:
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception:
                pytest.skip("Chromium not installed for Playwright")
            ctx = browser.new_context()
            page = ctx.new_page()

            # Invalid credentials -> inline error, still on login.html.
            page.goto(base + "/login.html", wait_until="networkidle")
            page.fill("#email", "uiadmin@corridoriq.com")
            page.fill("#password", "wrong-password")
            page.click("#loginBtn")
            page.wait_for_selector("#loginMsg.error", timeout=5000)
            assert page.url.endswith("/login.html")
            # The stock http.server 501 page would read "Unsupported method ('POST')".
            assert "Unsupported method" not in page.content()

            # Valid credentials -> redirect to the role landing page + session cookie.
            page.fill("#password", "UiAdmin123")
            page.click("#loginBtn")
            page.wait_for_url("**/admin-dashboard.html", timeout=8000)
            names = {c["name"] for c in ctx.cookies()}
            assert server_mod.COOKIE in names
            browser.close()
    except Exception as exc:  # environment without a usable browser
        if "Executable doesn't exist" in str(exc) or "chromium" in str(exc).lower():
            pytest.skip(f"Playwright browser unavailable: {exc}")
        raise
