"""CEO Agent belongs to the business owner, not to administration.

owner.ceo_agent is granted only by the server-side grant command (the "owner"
role). admin.system never implies it. Every check here is made against the
backend: the API, the page files, and the admin API that could otherwise be
used to grant it.
"""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.auth import service as auth
from pipeline.auth.grant_owner import OwnerGrantError, grant_owner, revoke_owner
from pipeline.auth.rbac import (
    CEO_AGENT_PERMISSION,
    OWNER_ONLY_PERMISSIONS,
    OWNER_ROLE,
    PERMISSIONS,
    ROLES,
    AuthzError,
    has_permission,
    load_user_permissions,
    require_ceo_owner,
)
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import SCHEMA_PATH
from pipeline.crm import admin as crm_admin

REPO = Path(__file__).resolve().parents[2]

PASSWORD = "CeoTestPass123"
# email -> roles. Everyone except "owner" must be refused.
ACCOUNTS = {
    "owner@corridoriq.com": ["admin", "owner"],
    "admin@corridoriq.com": ["admin"],
    "manager@corridoriq.com": ["sales_manager"],
    "rep@corridoriq.com": ["sales_representative"],
    "estimator@corridoriq.com": ["estimator"],
    "reader@corridoriq.com": ["read_only"],
    "fulfill@corridoriq.com": ["fulfillment_user"],
    "contractor@corridoriq.com": ["contractor_owner"],
}
DENIED = [email for email in ACCOUNTS if email != "owner@corridoriq.com"]


def _db(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    return c


def _org(c, slug="corridoriq"):
    return c.execute("SELECT id FROM organizations WHERE slug=?", (slug,)).fetchone()["id"]


def _ctx(c, email):
    row = c.execute("SELECT * FROM users WHERE normalized_email=?", (email,)).fetchone()
    return auth.build_user_context(c, row)


@pytest.fixture()
def db_file(tmp_path):
    path = tmp_path / "ceo_owner.db"
    c = _db(path)
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(c)
    org = _org(c)
    for email, roles in ACCOUNTS.items():
        auth.create_user(c, organization_id=org, email=email, password=PASSWORD,
                         role_names=roles, must_change_password=False)
    c.commit()
    c.close()
    return path


@pytest.fixture()
def conn(db_file):
    c = _db(db_file)
    yield c
    c.close()


# ---------------------------------------------------------------- RBAC model


def test_admin_role_does_not_include_owner_permissions():
    assert CEO_AGENT_PERMISSION in PERMISSIONS
    assert CEO_AGENT_PERMISSION in OWNER_ONLY_PERMISSIONS
    assert not (ROLES["admin"]["permissions"] & OWNER_ONLY_PERMISSIONS)
    for name, spec in ROLES.items():
        if name != OWNER_ROLE:
            assert not (spec["permissions"] & OWNER_ONLY_PERMISSIONS), name
    # The owner role is additive: it grants only the owner capability.
    assert ROLES[OWNER_ROLE]["permissions"] == set(OWNER_ONLY_PERMISSIONS)


def test_admin_system_is_not_weakened_and_does_not_imply_owner(conn):
    admin = _ctx(conn, "admin@corridoriq.com")
    assert admin["permissions"] == set(PERMISSIONS) - OWNER_ONLY_PERMISSIONS
    for key in set(PERMISSIONS) - OWNER_ONLY_PERMISSIONS:
        assert has_permission(admin, key), key
    assert not has_permission(admin, CEO_AGENT_PERMISSION)
    # Even a hand-built context holding only admin.system is refused.
    assert not has_permission({"permissions": {"admin.system"}}, CEO_AGENT_PERMISSION)
    owner = _ctx(conn, "owner@corridoriq.com")
    assert has_permission(owner, CEO_AGENT_PERMISSION)
    assert owner["permissions"] == set(PERMISSIONS)


def test_every_non_owner_role_is_refused_by_the_service_check(conn):
    for email in DENIED:
        with pytest.raises(AuthzError):
            require_ceo_owner(conn, _ctx(conn, email))
    require_ceo_owner(conn, _ctx(conn, "owner@corridoriq.com"))


def test_owner_grant_outside_corridoriq_org_is_refused(conn):
    """Defense in depth: the permission alone is not enough outside CorridorIQ's org."""
    now = "2026-10-06T00:00:00+00:00"
    conn.execute("INSERT INTO organizations (name, slug, is_active, created_at, updated_at) "
                 "VALUES ('Supply House', 'supply-house', 1, ?, ?)", (now, now))
    other = _org(conn, "supply-house")
    auth.create_user(conn, organization_id=other, email="boss@supply.example", password=PASSWORD,
                     role_names=["admin", "owner"], must_change_password=False)
    ctx = _ctx(conn, "boss@supply.example")
    assert has_permission(ctx, CEO_AGENT_PERMISSION)
    with pytest.raises(AuthzError):
        require_ceo_owner(conn, ctx)


def test_authorization_code_has_no_hard_coded_owner_identity():
    authz = [REPO / "pipeline" / "auth" / name for name in
             ("rbac.py", "grant_owner.py", "seed.py", "service.py", "sessions.py")]
    authz += [REPO / "pipeline" / "api" / "server.py", REPO / "pipeline" / "crm" / "admin.py",
              REPO / "ceo-morning-brief.js", REPO / "portal-common.js"]
    for path in authz:
        text = path.read_text(encoding="utf-8").lower()
        assert "archie" not in text, path.name
        assert not re.search(r"[a-z0-9._-]+@corridoriq\.pro", text), path.name


# ---------------------------------------------------------------- admin API


def test_admin_api_cannot_grant_the_owner_role(conn):
    admin = _ctx(conn, "admin@corridoriq.com")
    with pytest.raises(AuthzError):
        crm_admin.create_user(conn, admin, {"email": "new@corridoriq.com", "roles": ["owner"]})
    target = conn.execute("SELECT id FROM users WHERE normalized_email='admin@corridoriq.com'").fetchone()["id"]
    with pytest.raises(AuthzError):  # self-escalation
        crm_admin.update_user(conn, admin, target, {"roles": ["admin", "owner"]})
    assert not has_permission(_ctx(conn, "admin@corridoriq.com"), CEO_AGENT_PERMISSION)
    # Even the owner cannot hand the role out through the API.
    owner = _ctx(conn, "owner@corridoriq.com")
    with pytest.raises(AuthzError):
        crm_admin.update_user(conn, owner, target, {"roles": ["admin", "owner"]})


def test_ordinary_admin_cannot_change_or_take_over_the_owner_account(conn):
    admin = _ctx(conn, "admin@corridoriq.com")
    owner_id = conn.execute("SELECT id FROM users WHERE normalized_email='owner@corridoriq.com'").fetchone()["id"]
    for change in ({"email": "attacker@corridoriq.com"}, {"is_active": False},
                   {"roles": ["admin"]}, {"display_name": "Not the owner"}):
        with pytest.raises(AuthzError):
            crm_admin.update_user(conn, admin, owner_id, change)
    row = conn.execute("SELECT email, is_active FROM users WHERE id=?", (owner_id,)).fetchone()
    assert row["email"] == "owner@corridoriq.com" and row["is_active"] == 1
    assert has_permission(_ctx(conn, "owner@corridoriq.com"), CEO_AGENT_PERMISSION)


def test_role_edits_through_the_api_never_drop_the_owner_role(conn):
    """No lockout: editing the owner's roles keeps the owner grant."""
    owner = _ctx(conn, "owner@corridoriq.com")
    out = crm_admin.update_user(conn, owner, owner["id"], {"roles": ["admin"]})
    assert OWNER_ROLE in out["roles"] and "admin" in out["roles"]
    assert has_permission(_ctx(conn, "owner@corridoriq.com"), CEO_AGENT_PERMISSION)


# ---------------------------------------------------------------- grant command


def test_grant_command_requires_an_active_corridoriq_admin(conn):
    with pytest.raises(OwnerGrantError):
        grant_owner(conn, "rep@corridoriq.com")
    with pytest.raises(OwnerGrantError):
        grant_owner(conn, "nobody@corridoriq.com")
    conn.execute("UPDATE users SET is_active=0 WHERE normalized_email='manager@corridoriq.com'")
    conn.execute("INSERT INTO user_roles (user_id, role_id, assigned_at) SELECT u.id, r.id, 'x' "
                 "FROM users u, roles r WHERE u.normalized_email='manager@corridoriq.com' AND r.name='admin'")
    conn.commit()
    with pytest.raises(OwnerGrantError):
        grant_owner(conn, "manager@corridoriq.com")
    assert not has_permission(_ctx(conn, "rep@corridoriq.com"), CEO_AGENT_PERMISSION)


def test_grant_dry_run_then_grant_then_revoke(conn):
    dry = grant_owner(conn, "admin@corridoriq.com", dry_run=True)
    assert dry["changed"] is False and dry["would_change"] is True
    assert not has_permission(_ctx(conn, "admin@corridoriq.com"), CEO_AGENT_PERMISSION)

    done = grant_owner(conn, "ADMIN@corridoriq.com")
    assert done["changed"] is True and OWNER_ROLE in done["roles"]
    assert has_permission(_ctx(conn, "admin@corridoriq.com"), CEO_AGENT_PERMISSION)
    assert grant_owner(conn, "admin@corridoriq.com")["changed"] is False  # idempotent
    audit = conn.execute("SELECT action FROM security_audit_log WHERE action='grant_owner_role'").fetchall()
    assert len(audit) == 1

    gone = revoke_owner(conn, "admin@corridoriq.com")
    assert gone["changed"] is True and OWNER_ROLE not in gone["roles"]
    assert not has_permission(_ctx(conn, "admin@corridoriq.com"), CEO_AGENT_PERMISSION)
    assert "admin" in gone["roles"]  # revoking owner leaves administration intact


# ---------------------------------------------------------------- HTTP


@pytest.fixture()
def http_server(db_file, tmp_path, monkeypatch):
    import pipeline.api.server as server_mod

    monkeypatch.setattr(server_mod, "_factory", lambda: _db(db_file))
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo-out"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.05)
    yield {"port": srv.server_address[1], "db": db_file}
    srv.shutdown()


def _req(port, method, path, body=None, cookie=None):
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    client.request(method, path, json.dumps(body) if body is not None else None, headers)
    response = client.getresponse()
    raw = response.read()
    result = (response.status, raw, response.getheader("Set-Cookie"), response.getheader("Location"))
    client.close()
    return result


def _login(port, email):
    status, _raw, cookie, _loc = _req(port, "POST", "/api/auth/login", {"email": email, "password": PASSWORD})
    assert status == 200, email
    return cookie.split(";")[0]


def test_ceo_api_refuses_every_role_but_the_owner(http_server):
    port = http_server["port"]
    status, raw, _c, _l = _req(port, "GET", "/api/admin/ceo-morning-brief")
    assert status == 401
    # A contractor pilot session is a different system and never reaches it.
    status, _raw, _c, _l = _req(port, "GET", "/api/admin/ceo-morning-brief",
                                cookie="corridoriq_pilot_session=contractor-token")
    assert status == 401
    for email in DENIED:
        cookie = _login(port, email)
        status, raw, _c, _l = _req(port, "GET", "/api/admin/ceo-morning-brief", cookie=cookie)
        assert status == 403, email
        body = json.loads(raw)
        assert body == {"error": f"missing permission: {CEO_AGENT_PERMISSION}"}, email
        # The query string cannot widen access either.
        status, _raw, _c, _l = _req(port, "GET", "/api/admin/ceo-morning-brief?organization=1&as=owner",
                                    cookie=cookie)
        assert status == 403, email
    owner = _login(port, "owner@corridoriq.com")
    status, raw, _c, _l = _req(port, "GET", "/api/admin/ceo-morning-brief", cookie=owner)
    assert status == 200
    body = json.loads(raw)
    assert body["execute"] is False and "available" in body


def test_ceo_page_files_are_served_only_to_the_owner(http_server):
    port = http_server["port"]
    for path in ("/ceo-morning-brief.html", "/ceo-morning-brief.js"):
        status, raw, _c, location = _req(port, "GET", path)
        assert status == 302 and location == "/login.html", path
        assert b"CIQ" not in raw and b"<html" not in raw.lower()
    for email in DENIED:
        cookie = _login(port, email)
        # Typed URL, percent-encoded, and re-cased forms are all refused.
        for path in ("/ceo-morning-brief.html", "/ceo-morning-brief.js",
                     "/ceo-morning-brief%2Ehtml", "/CEO-Morning-Brief.html"):
            status, raw, _c, _l = _req(port, "GET", path, cookie=cookie)
            assert status != 200, (email, path)
            assert b"<html" not in raw.lower() and b"CIQ.guard" not in raw, (email, path)
    owner = _login(port, "owner@corridoriq.com")
    status, raw, _c, _l = _req(port, "GET", "/ceo-morning-brief.html", cookie=owner)
    assert status == 200 and b"<html" in raw.lower()
    status, raw, _c, _l = _req(port, "GET", "/ceo-morning-brief.js", cookie=owner)
    assert status == 200 and b'CIQ.guard("owner.ceo_agent"' in raw
    # Other portal pages are unaffected.
    assert _req(port, "GET", "/login.html")[0] == 200


def test_denials_are_audited(http_server):
    port = http_server["port"]
    cookie = _login(port, "admin@corridoriq.com")
    _req(port, "GET", "/api/admin/ceo-morning-brief", cookie=cookie)
    _req(port, "GET", "/ceo-morning-brief.html", cookie=cookie)
    c = _db(http_server["db"])
    rows = c.execute("SELECT resource_id FROM security_audit_log WHERE event_type='permission_denied'").fetchall()
    c.close()
    denied = {r["resource_id"] for r in rows}
    assert {"/api/admin/ceo-morning-brief", "/ceo-morning-brief.html"} <= denied


def test_admin_http_api_cannot_grant_owner(http_server):
    port = http_server["port"]
    cookie = _login(port, "admin@corridoriq.com")
    status, _raw, _c, _l = _req(port, "POST", "/api/admin/users",
                                {"email": "sneaky@corridoriq.com", "roles": ["owner"]}, cookie=cookie)
    assert status == 403
    c = _db(http_server["db"])
    me = c.execute("SELECT id FROM users WHERE normalized_email='admin@corridoriq.com'").fetchone()["id"]
    owner_id = c.execute("SELECT id FROM users WHERE normalized_email='owner@corridoriq.com'").fetchone()["id"]
    c.close()
    status, _raw, _c, _l = _req(port, "PATCH", f"/api/admin/users/{me}",
                                {"roles": ["admin", "owner"]}, cookie=cookie)
    assert status == 403
    status, _raw, _c, _l = _req(port, "PATCH", f"/api/admin/users/{owner_id}",
                                {"email": "attacker@corridoriq.com"}, cookie=cookie)
    assert status == 403
    status, _raw, _c, _l = _req(port, "GET", "/api/admin/ceo-morning-brief", cookie=cookie)
    assert status == 403


def test_ceo_artifacts_are_not_reachable_through_reports_or_static_files():
    """The brief JSON lives under reports/generated/ceo/. Neither the reports
    download (reports.view) nor the static server may hand it out."""
    from pipeline.api.server import static_target
    from pipeline.reports.catalog import safe_generated_path

    for name in ("ceo/intelligence/latest_daily_brief.json", "ceo\\latest_brief.md",
                 "../generated/ceo/latest_brief.md", "ceo"):
        assert safe_generated_path(name) is None, name
    for path in ("/reports/generated/ceo/intelligence/latest_daily_brief.json",
                 "/reports/generated/ceo/latest_brief.md", "/agents/ceo/company/launch.json"):
        assert static_target(path) == (None, None), path


def test_menu_hides_ceo_from_ordinary_admins():
    """The browser mirrors the server: owner.* is never implied by admin.system."""
    js = (REPO / "portal-common.js").read_text(encoding="utf-8")
    has_perm = js[js.index("CIQ.hasPerm = "):js.index("CIQ.logout")]
    assert re.search(r'indexOf\("owner\."\) === 0\) return perms\.includes\(key\)', has_perm)
