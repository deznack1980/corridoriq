"""Sprint 5 tests — authentication, RBAC, CRM, and intelligence protection.

These exercise the backend service layer (where authorization is enforced) plus
an HTTP integration check confirming unauthenticated/unauthorized requests are
rejected by the server, not merely hidden in the UI.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

import pytest

from pipeline.auth import service as auth
from pipeline.auth.passwords import hash_password, verify_password
from pipeline.auth.rbac import AuthzError, can_access_company, has_permission
from pipeline.auth.seed import seed_auth
from pipeline.auth.service import AuthError, change_password, get_current_user, login
from pipeline.auth.sessions import get_session
from pipeline.config.settings import SCHEMA_PATH
from pipeline.crm import admin as crm_admin
from pipeline.crm import service as crm


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) "
              "VALUES ('phoenix_az','Phoenix','AZ','connected')")
    seed_auth(c)
    return c


def _org(c, slug="corridoriq"):
    return c.execute("SELECT id FROM organizations WHERE slug=?", (slug,)).fetchone()["id"]


def _make_company(c, name="ACME", city="PHOENIX"):
    now = _now()
    cur = c.execute(
        "INSERT INTO companies (normalized_name, display_name, city, state, "
        "lifecycle_state, created_at, updated_at) VALUES (?,?,?,?,'active',?,?)",
        (name.upper(), name, city, "AZ", now, now))
    c.commit()
    return cur.lastrowid


def _ctx(c, user_id):
    row = c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return auth.build_user_context(c, row)


@pytest.fixture()
def env(conn):
    org = _org(conn)
    admin_id = auth.create_user(conn, organization_id=org, email="admin@corridoriq.com",
                                password="AdminPass123", role_names=["admin"],
                                must_change_password=False)
    mgr_id = auth.create_user(conn, organization_id=org, email="mgr@corridoriq.com",
                              password="MgrPass123", role_names=["sales_manager"],
                              must_change_password=False)
    rep_id = auth.create_user(conn, organization_id=org, email="rep@corridoriq.com",
                              password="RepPass123", role_names=["sales_representative"],
                              must_change_password=True)
    rep2_id = auth.create_user(conn, organization_id=org, email="rep2@corridoriq.com",
                               password="Rep2Pass123", role_names=["sales_representative"],
                               must_change_password=False)
    ro_id = auth.create_user(conn, organization_id=org, email="ro@corridoriq.com",
                             password="ReadPass123", role_names=["read_only"],
                             must_change_password=False)
    ful_id = auth.create_user(conn, organization_id=org, email="ful@corridoriq.com",
                              password="FulPass123", role_names=["fulfillment_user"],
                              must_change_password=False)
    company = _make_company(conn)
    return {
        "conn": conn, "org": org, "company": company,
        "admin": _ctx(conn, admin_id), "mgr": _ctx(conn, mgr_id),
        "rep": _ctx(conn, rep_id), "rep2": _ctx(conn, rep2_id),
        "ro": _ctx(conn, ro_id), "ful": _ctx(conn, ful_id),
        "ids": {"admin": admin_id, "mgr": mgr_id, "rep": rep_id,
                "rep2": rep2_id, "ro": ro_id, "ful": ful_id},
    }


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def test_password_hash_roundtrip():
    h = hash_password("Sup3rSecret!")
    assert h != "Sup3rSecret!" and "Sup3rSecret" not in h
    assert verify_password("Sup3rSecret!", h)
    assert not verify_password("wrong", h)


def test_valid_login(env):
    c = env["conn"]
    token, user = login(c, "admin@corridoriq.com", "AdminPass123")
    assert user["email"] == "admin@corridoriq.com"
    assert user["default_landing_page"] == "admin-dashboard.html"
    assert get_session(c, token) is not None


def test_invalid_login_generic_error(env):
    c = env["conn"]
    with pytest.raises(AuthError):
        login(c, "admin@corridoriq.com", "nope")
    with pytest.raises(AuthError):
        login(c, "ghost@corridoriq.com", "whatever")


def test_disabled_user_cannot_login(env):
    c = env["conn"]
    c.execute("UPDATE users SET is_active=0 WHERE id=?", (env["ids"]["rep2"],))
    c.commit()
    with pytest.raises(AuthError):
        login(c, "rep2@corridoriq.com", "Rep2Pass123")


def test_account_lockout(env):
    c = env["conn"]
    for _ in range(5):
        with pytest.raises(AuthError):
            login(c, "rep2@corridoriq.com", "bad")
    row = c.execute("SELECT locked_until FROM users WHERE id=?", (env["ids"]["rep2"],)).fetchone()
    assert row["locked_until"] is not None
    # Even the correct password is rejected while locked.
    with pytest.raises(AuthError):
        login(c, "rep2@corridoriq.com", "Rep2Pass123")


def test_logout_revokes_session(env):
    c = env["conn"]
    token, _ = login(c, "admin@corridoriq.com", "AdminPass123")
    assert get_current_user(c, token) is not None
    auth.logout(c, token, user_id=env["ids"]["admin"])
    assert get_current_user(c, token) is None


def test_forced_password_change_flag(env):
    c = env["conn"]
    _, user = login(c, "rep@corridoriq.com", "RepPass123")
    assert user["must_change_password"] is True


def test_password_change_revokes_sessions(env):
    c = env["conn"]
    token, _ = login(c, "rep2@corridoriq.com", "Rep2Pass123")
    change_password(c, env["ids"]["rep2"], "Rep2Pass123", "BrandNewPass123")
    assert get_current_user(c, token) is None
    token2, _ = login(c, "rep2@corridoriq.com", "BrandNewPass123")
    assert token2


def test_expired_session_rejected(env):
    c = env["conn"]
    token, _ = login(c, "admin@corridoriq.com", "AdminPass123")
    c.execute("UPDATE sessions SET expires_at=? WHERE token=?",
              ("2000-01-01T00:00:00+00:00", token))
    c.commit()
    assert get_current_user(c, token) is None


def test_password_hash_never_serialized(env):
    users = crm_admin.list_users(env["conn"], env["admin"])
    assert users
    for u in users:
        assert "password_hash" not in u
        assert "normalized_email" not in u


# ---------------------------------------------------------------------------
# Authorization / RBAC
# ---------------------------------------------------------------------------

def test_admin_has_all_permissions(env):
    assert has_permission(env["admin"], "admin.system")
    assert has_permission(env["admin"], "knowledge.approve")
    assert has_permission(env["admin"], "supplier_pricing.manage")


def test_sales_manager_permissions(env):
    m = env["mgr"]
    assert has_permission(m, "companies.assign")
    assert has_permission(m, "companies.view")
    assert not has_permission(m, "knowledge.approve")
    assert not has_permission(m, "supplier_pricing.manage")


def test_sales_rep_limitations(env):
    r = env["rep"]
    assert has_permission(r, "companies.view_assigned")
    assert has_permission(r, "crm.activities.create")
    assert not has_permission(r, "companies.view")
    assert not has_permission(r, "companies.assign")
    assert not has_permission(r, "users.view")
    assert not has_permission(r, "knowledge.view")
    assert not has_permission(r, "supplier_pricing.view")


def test_read_only_limitations(env):
    ro = env["ro"]
    assert has_permission(ro, "crm.activities.view")
    assert not has_permission(ro, "crm.activities.create")
    assert not has_permission(ro, "crm.relationships.update")


def test_fulfillment_no_contractor_intelligence(env):
    ful = env["ful"]
    assert not has_permission(ful, "companies.view")
    assert not has_permission(ful, "companies.view_assigned")
    assert not can_access_company(env["conn"], ful, env["company"])


def test_record_level_access_enforced(env):
    c, company = env["conn"], env["company"]
    # rep not assigned yet -> denied
    assert not can_access_company(c, env["rep"], company)
    with pytest.raises(AuthzError):
        crm.get_relationship(c, env["rep"], company)
    # manager assigns to rep -> now allowed
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"])
    rep = _ctx(c, env["ids"]["rep"])
    assert can_access_company(c, rep, company)
    # rep2 still cannot see it
    assert not can_access_company(c, env["rep2"], company)


def test_admin_sees_all_companies(env):
    assert can_access_company(env["conn"], env["admin"], env["company"])


def test_rep_cannot_self_assign(env):
    with pytest.raises(AuthzError):
        crm.assign_company(env["conn"], env["rep"], env["company"], env["ids"]["rep"])


def test_organization_isolation(env):
    c = env["conn"]
    now = _now()
    cur = c.execute("INSERT INTO organizations (name, slug, is_active, created_at, updated_at) "
                    "VALUES ('Other','other',1,?,?)", (now, now))
    other_org = cur.lastrowid
    c.commit()
    other_admin = auth.create_user(c, organization_id=other_org, email="a@other.com",
                                   password="OtherPass123", role_names=["admin"],
                                   must_change_password=False)
    # Assign company in default org.
    crm.assign_company(c, env["mgr"], env["company"], env["ids"]["rep"])
    # Other-org admin lists their own companies -> empty (isolation).
    result = crm.list_my_companies(c, _ctx(c, other_admin))
    assert result["total"] == 0
    # Other-org admin cannot assign to a user from a different org.
    with pytest.raises(crm.ValidationError):
        crm.assign_company(c, _ctx(c, other_admin), env["company"], env["ids"]["rep"])


# ---------------------------------------------------------------------------
# CRM
# ---------------------------------------------------------------------------

def test_relationship_and_assignment_history(env):
    c, company = env["conn"], env["company"]
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"], reason="initial")
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep2"], reason="reassign")
    hist = crm.assignment_history(c, env["mgr"], company)
    assert len(hist) == 2
    assert hist[0]["new_user_id"] == env["ids"]["rep2"]
    assert hist[0]["previous_user_id"] == env["ids"]["rep"]


def test_one_active_relationship(env):
    c, company = env["conn"], env["company"]
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"])
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep2"])
    n = c.execute("SELECT COUNT(*) AS n FROM crm_company_relationships "
                  "WHERE organization_id=? AND company_id=?", (env["org"], company)).fetchone()["n"]
    assert n == 1


def test_activity_creation_and_audit(env):
    c, company = env["conn"], env["company"]
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"])
    rep = _ctx(c, env["ids"]["rep"])
    act = crm.create_activity(c, rep, company, {"activity_type": "call",
                                                "activity_outcome": "spoke_with_contact",
                                                "subject": "intro"})
    assert act["user_id"] == env["ids"]["rep"]
    assert act["activity_at"]
    audited = c.execute("SELECT COUNT(*) AS n FROM security_audit_log "
                        "WHERE event_type='activity_created'").fetchone()["n"]
    assert audited == 1


def test_status_change_creates_timeline_event(env):
    c, company = env["conn"], env["company"]
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"])
    rep = _ctx(c, env["ids"]["rep"])
    crm.update_relationship(c, rep, company, {"relationship_status": "qualified"})
    ev = c.execute("SELECT COUNT(*) AS n FROM crm_activities WHERE activity_type='status_change'").fetchone()["n"]
    assert ev == 1
    audited = c.execute("SELECT COUNT(*) AS n FROM security_audit_log "
                        "WHERE event_type='relationship_status_changed'").fetchone()["n"]
    assert audited == 1


def test_followup_scheduling(env):
    c, company = env["conn"], env["company"]
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"])
    rep = _ctx(c, env["ids"]["rep"])
    crm.create_activity(c, rep, company, {"activity_type": "call", "next_followup_at": "2026-03-01"})
    rel = crm.get_relationship(c, rep, company)
    assert rel["next_followup_at"] == "2026-03-01"
    assert rel["last_contact_at"] is not None


def test_task_lifecycle(env):
    c = env["conn"]
    rep = env["rep"]
    task = crm.create_task(c, rep, {"title": "Call back", "priority": "high"})
    assert task["status"] == "open"
    done = crm.update_task(c, rep, task["id"], {"status": "completed"})
    assert done["status"] == "completed" and done["completed_at"]
    audited = c.execute("SELECT COUNT(*) AS n FROM security_audit_log "
                        "WHERE event_type='task_completed'").fetchone()["n"]
    assert audited == 1


def test_activity_edit_keeps_revision(env):
    c, company = env["conn"], env["company"]
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"])
    rep = _ctx(c, env["ids"]["rep"])
    act = crm.create_activity(c, rep, company, {"activity_type": "note", "notes": "v1"})
    crm.update_activity(c, rep, act["id"], {"notes": "v2"})
    rev = c.execute("SELECT COUNT(*) AS n FROM crm_activity_revisions WHERE activity_id=?",
                    (act["id"],)).fetchone()["n"]
    assert rev == 1


def test_read_only_cannot_create_activity(env):
    c, company = env["conn"], env["company"]
    crm.assign_company(c, env["mgr"], company, env["ids"]["ro"])
    ro = _ctx(c, env["ids"]["ro"])
    with pytest.raises(AuthzError):
        crm.create_activity(c, ro, company, {"activity_type": "call"})


# ---------------------------------------------------------------------------
# Intelligence protection
# ---------------------------------------------------------------------------

def _snapshot_intel(c, company):
    return (
        c.execute("SELECT COUNT(*) AS n FROM permits").fetchone()["n"],
        c.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"],
    )


def test_sales_activity_does_not_touch_intelligence(env):
    c, company = env["conn"], env["company"]
    # seed a permit + project + intelligence row
    now = _now()
    cur = c.execute("INSERT INTO permits (jurisdiction, permit_number, status, "
                    "contractor_company_id, first_seen_at, last_updated_at) "
                    "VALUES ('phoenix_az','P1','Permit Issued',?,?,?)", (company, now, now))
    permit_id = cur.lastrowid
    c.execute("INSERT INTO projects (permit_id, jurisdiction, opportunity_score, "
              "contractor_company_id) VALUES (?, 'phoenix_az', 88.0, ?)", (permit_id, company))
    c.execute("INSERT INTO company_intelligence (company_id, company_priority_score, "
              "model_version) VALUES (?, 55.0, 'company-metrics-v1')", (company,))
    c.commit()

    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"])
    rep = _ctx(c, env["ids"]["rep"])
    before_score = c.execute("SELECT opportunity_score FROM projects WHERE permit_id=?",
                             (permit_id,)).fetchone()["opportunity_score"]
    before_priority = c.execute("SELECT company_priority_score FROM company_intelligence "
                                "WHERE company_id=?", (company,)).fetchone()["company_priority_score"]

    crm.create_activity(c, rep, company, {"activity_type": "call"})
    crm.update_relationship(c, rep, company, {"relationship_status": "qualified"})

    after_score = c.execute("SELECT opportunity_score FROM projects WHERE permit_id=?",
                            (permit_id,)).fetchone()["opportunity_score"]
    after_priority = c.execute("SELECT company_priority_score FROM company_intelligence "
                               "WHERE company_id=?", (company,)).fetchone()["company_priority_score"]
    assert before_score == after_score == 88.0
    assert before_priority == after_priority == 55.0


def test_sales_rep_lacks_intelligence_and_knowledge_permissions(env):
    r = env["rep"]
    for perm in ("knowledge.view", "knowledge.review", "knowledge.approve",
                 "supplier_pricing.view", "supplier_pricing.manage", "roles.manage",
                 "admin.system"):
        assert not has_permission(r, perm)


def test_rep_cannot_manage_users(env):
    with pytest.raises(AuthzError):
        crm_admin.list_users(env["conn"], env["rep"])
    with pytest.raises(AuthzError):
        crm_admin.create_user(env["conn"], env["rep"], {"email": "x@corridoriq.com"})


def test_company_intelligence_serializer_excludes_source_ids(env):
    from pipeline.crm import serializers
    c, company = env["conn"], env["company"]
    c.execute("UPDATE companies SET source_system='secret_sys', source_record_id='xyz' WHERE id=?",
              (company,))
    c.commit()
    crm.assign_company(c, env["mgr"], company, env["ids"]["rep"])
    detail = crm.get_company_detail(c, _ctx(c, env["ids"]["rep"]), company)
    assert "source_system" not in detail["company"]
    assert "source_record_id" not in detail["company"]


# ---------------------------------------------------------------------------
# HTTP integration — enforcement is server-side, not UI-only
# ---------------------------------------------------------------------------

@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    import pipeline.api.server as server_mod

    db_file = tmp_path / "s5_http.db"

    def factory():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        cc.execute("PRAGMA foreign_keys = ON")
        return cc

    # Build the DB once.
    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) "
              "VALUES ('phoenix_az','Phoenix','AZ','connected')")
    seed_auth(c)
    org = _org(c)
    auth.create_user(c, organization_id=org, email="httpadmin@corridoriq.com",
                     password="HttpAdmin123", role_names=["admin"], must_change_password=False)
    auth.create_user(c, organization_id=org, email="httpmcp@corridoriq.com",
                     password="HttpMcp123", role_names=["admin"], must_change_password=True)
    rep_id = auth.create_user(c, organization_id=org, email="httprep@corridoriq.com",
                              password="HttpRep123", role_names=["sales_representative"],
                              must_change_password=False)
    company = _make_company(c, "HTTPCo")
    c.commit()
    c.close()

    monkeypatch.setattr(server_mod, "_factory", factory)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)
    yield {"port": port, "company": company, "rep_id": rep_id}
    srv.shutdown()


def _req(port, method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    resp = conn.getresponse()
    raw = resp.read()
    set_cookie = resp.getheader("Set-Cookie")
    conn.close()
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    return resp.status, data, set_cookie


def test_http_requires_authentication(http_server):
    port = http_server["port"]
    status, _, _ = _req(port, "GET", "/api/sales/dashboard")
    assert status == 401


def test_http_login_and_permission_denied(http_server):
    port = http_server["port"]
    status, data, set_cookie = _req(port, "POST", "/api/auth/login",
                                    {"email": "httprep@corridoriq.com", "password": "HttpRep123"})
    assert status == 200
    assert "password_hash" not in json.dumps(data)
    cookie = set_cookie.split(";")[0]
    assert "HttpOnly" in set_cookie
    # Rep hitting an admin route -> 403 from the backend.
    status, _, _ = _req(port, "GET", "/api/admin/users", cookie=cookie)
    assert status == 403
    # Rep accessing an unassigned company -> 403.
    status, _, _ = _req(port, "GET", f"/api/sales/companies/{http_server['company']}", cookie=cookie)
    assert status == 403


def test_http_login_sets_httponly_cookie(http_server):
    port = http_server["port"]
    _, _, set_cookie = _req(port, "POST", "/api/auth/login",
                            {"email": "httpadmin@corridoriq.com", "password": "HttpAdmin123"})
    assert "HttpOnly" in set_cookie
    assert "SameSite=Lax" in set_cookie


def test_http_must_change_password_blocks_protected_routes(http_server):
    port = http_server["port"]
    status, data, set_cookie = _req(port, "POST", "/api/auth/login",
                                    {"email": "httpmcp@corridoriq.com", "password": "HttpMcp123"})
    assert status == 200 and data["user"]["must_change_password"] is True
    cookie = set_cookie.split(";")[0]
    assert _req(port, "GET", "/api/auth/me", cookie=cookie)[0] == 200
    status, body, _ = _req(port, "GET", "/api/sales/dashboard", cookie=cookie)
    assert status == 403 and body["error"] == "password_change_required"
    status, body, _ = _req(port, "GET", "/api/admin/users", cookie=cookie)
    assert status == 403 and body["error"] == "password_change_required"
    assert _req(port, "POST", "/api/auth/logout", {}, cookie=cookie)[0] == 200
    status, data, set_cookie = _req(port, "POST", "/api/auth/login",
                                    {"email": "httpmcp@corridoriq.com", "password": "HttpMcp123"})
    cookie = set_cookie.split(";")[0]
    assert _req(port, "POST", "/api/auth/change-password",
                {"old_password": "HttpMcp123", "new_password": "HttpMcp4567"}, cookie=cookie)[0] == 200
    assert _req(port, "GET", "/api/auth/me", cookie=cookie)[0] == 401
    status, data, set_cookie = _req(port, "POST", "/api/auth/login",
                                    {"email": "httpmcp@corridoriq.com", "password": "HttpMcp4567"})
    assert status == 200 and data["user"]["must_change_password"] is False
    cookie = set_cookie.split(";")[0]
    assert _req(port, "GET", "/api/sales/dashboard", cookie=cookie)[0] == 200
