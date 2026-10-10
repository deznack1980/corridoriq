"""Public exploration, self-service registration, verification gates, and
role isolation. Authorization is asserted on the server — not the UI.
"""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.auth import mailer, rate_limit, tokens
from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import PROJECT_ROOT, SCHEMA_PATH
from pipeline.crm import admin as crm_admin

REPO = Path(PROJECT_ROOT)


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).isoformat(timespec="seconds")


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "onboard.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(conn)
    yield conn
    conn.close()


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    import pipeline.api.server as server_mod

    db_file = tmp_path / "onboard_http.db"
    mailer.clear_outbox()
    rate_limit.LIMITER.reset()

    def factory():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        cc.execute("PRAGMA foreign_keys = ON")
        return cc

    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(c)
    org = c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    auth.create_user(
        c, organization_id=org, email="legacy@corridoriq.com",
        password="LegacyPass123", role_names=["sales_representative"],
        must_change_password=False,
    )
    auth.create_user(
        c, organization_id=org, email="onboard-admin@corridoriq.com",
        password="OnboardAdmin123", role_names=["admin"],
        must_change_password=False,
    )
    c.commit()
    c.close()

    monkeypatch.setattr(server_mod, "_factory", factory)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(0.05)
    yield {"port": port, "db_file": db_file, "factory": factory}
    srv.shutdown()
    mailer.clear_outbox()
    rate_limit.LIMITER.reset()


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
        data = {"_raw": raw.decode("utf-8", "replace")}
    return resp.status, data, set_cookie


def _cookie(set_cookie):
    return set_cookie.split(";")[0] if set_cookie else None


def _token_from_mail(purpose="email_verification"):
    for rec in reversed(mailer.OUTBOX):
        if rec["purpose"] != purpose:
            continue
        m = re.search(r"token=([A-Za-z0-9_\-]+)", rec["text"])
        if m:
            return m.group(1)
    raise AssertionError(f"no {purpose} token in outbox")


def _register_contractor(port, email="owner@example.test"):
    status, data, sc = _req(port, "POST", "/api/auth/register/contractor", {
        "name": "Alex Rivera",
        "business_name": "Rivera Plumbing",
        "email": email,
        "password": "SecurePass123",
    })
    assert status == 201, data
    return _cookie(sc), data["user"]


def _register_supplier(port, email="counter@example.test"):
    status, data, sc = _req(port, "POST", "/api/auth/register/supplier", {
        "name": "Jordan Lee",
        "business_name": "Valley Pipe Supply",
        "email": email,
        "password": "SecurePass123",
        "phone": "480-555-0199",
        "business_category": "plumbing",
    })
    assert status == 201, data
    return _cookie(sc), data["user"]


# ---------------------------------------------------------------------------
# 1. Anonymous public exploration
# ---------------------------------------------------------------------------

def test_anonymous_public_preview(http_server):
    port = http_server["port"]
    status, data, _ = _req(port, "GET", "/api/public/preview")
    assert status == 200
    assert data["kind"] == "public_preview"
    assert "synthetic" in data["disclaimer"].lower() or "demonstration" in data["disclaimer"].lower()
    assert data["opportunities"]
    blob = json.dumps(data)
    assert "password_hash" not in blob
    assert "token_hash" not in blob
    assert not re.search(r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b", blob)
    assert "opportunity_score" not in blob
    assert "@" not in json.dumps(data["opportunities"])


def test_anonymous_cannot_read_sales_or_admin(http_server):
    port = http_server["port"]
    assert _req(port, "GET", "/api/sales/dashboard")[0] == 401
    assert _req(port, "GET", "/api/admin/users")[0] == 401
    assert _req(port, "GET", "/api/contractor/dashboard")[0] == 401


def test_public_pages_advertise_signup_and_signin():
    for name in ("home.html", "for-contractors.html", "for-suppliers.html", "explore.html"):
        html = (REPO / name).read_text(encoding="utf-8")
        assert "register-contractor.html" in html, name
        assert "register-supplier.html" in html, name
        assert "login.html" in html, name
        assert "Explore Platform" in html or "explore.html" in html, name


def test_login_page_has_role_specific_links():
    html = (REPO / "login.html").read_text(encoding="utf-8")
    assert "register-contractor.html" in html
    assert "register-supplier.html" in html
    assert "reset-password.html" in html
    assert "/api/auth/login" in html


# ---------------------------------------------------------------------------
# 2–4. Contractor registration, duplicates, unverified login
# ---------------------------------------------------------------------------

def test_contractor_account_creation(http_server):
    port = http_server["port"]
    cookie, user = _register_contractor(port)
    assert cookie
    assert "contractor" in user["roles"]
    assert user["account_kind"] == "contractor"
    assert user["account_state"] == "ACTIVE"
    assert user["email_verified"] is False
    assert user["default_landing_page"] == "contractor-welcome.html"
    assert "admin" not in user["roles"]
    assert "admin.system" not in user["permissions"]
    status, dash, _ = _req(port, "GET", "/api/contractor/dashboard", cookie=cookie)
    assert status == 200
    assert dash["welcome"] is True
    assert dash["email_verified"] is False


def test_duplicate_registration(http_server):
    port = http_server["port"]
    _register_contractor(port, "dup@example.test")
    mailer.clear_outbox()
    status, data, _ = _req(port, "POST", "/api/auth/register/contractor", {
        "name": "Alex Rivera", "business_name": "Rivera Plumbing",
        "email": "dup@example.test", "password": "SecurePass123",
    })
    assert status == 409
    assert "already" in data["error"].lower() or "could not create" in data["error"].lower()
    assert data["error"].lower().find("exists") == -1 or "sign in" in data["error"].lower()


def test_unverified_contractor_can_login_and_explore(http_server):
    port = http_server["port"]
    _register_contractor(port, "again@example.test")
    status, data, sc = _req(port, "POST", "/api/auth/login", {
        "email": "again@example.test", "password": "SecurePass123",
    })
    assert status == 200
    assert data["user"]["email_verified"] is False
    cookie = _cookie(sc)
    assert _req(port, "GET", "/api/contractor/dashboard", cookie=cookie)[0] == 200
    assert _req(port, "PATCH", "/api/contractor/profile",
                {"business_name": "Rivera Plumbing Co"}, cookie=cookie)[0] == 200


# ---------------------------------------------------------------------------
# 5–7. Protected denial, verification, verified access
# ---------------------------------------------------------------------------

def test_unverified_contractor_denied_protected_features(http_server):
    port = http_server["port"]
    cookie, _ = _register_contractor(port, "gate@example.test")
    for method, path in (
        ("POST", "/api/contractor/rfq"),
        ("GET", "/api/contractor/intelligence"),
        ("GET", "/api/contractor/quotes"),
        ("GET", "/api/sales/dashboard"),
        ("GET", "/api/admin/users"),
    ):
        status, data, _ = _req(port, method, path, {} if method == "POST" else None, cookie=cookie)
        assert status in (403, 401), (path, status, data)
        if path.startswith("/api/contractor/") and status == 403:
            assert "verification" in data["error"].lower()


def test_email_verification_and_verified_access(http_server):
    port = http_server["port"]
    cookie, user = _register_contractor(port, "verify@example.test")
    raw = _token_from_mail()
    status, data, _ = _req(port, "POST", "/api/auth/verify-email", {"token": raw})
    assert status == 200
    assert data["user"]["email_verified"] is True
    status, _, _ = _req(port, "POST", "/api/contractor/rfq", {"title": "demo"}, cookie=cookie)
    assert status == 201
    assert _req(port, "GET", "/api/contractor/intelligence", cookie=cookie)[0] == 200
    reused = _req(port, "POST", "/api/auth/verify-email", {"token": raw})
    assert reused[0] == 400


# ---------------------------------------------------------------------------
# 8–9. Supplier registration and approval
# ---------------------------------------------------------------------------

def test_supplier_registration_and_approval_requirements(http_server):
    port = http_server["port"]
    cookie, user = _register_supplier(port)
    assert user["account_kind"] == "supplier"
    assert user["account_state"] == "PENDING_EMAIL_VERIFICATION"
    assert user["default_landing_page"] == "supplier-welcome.html"
    assert _req(port, "GET", "/api/supplier/dashboard", cookie=cookie)[0] == 200
    assert _req(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 403
    assert _req(port, "POST", "/api/supplier/quotes", {"note": "x"}, cookie=cookie)[0] == 403
    assert _req(port, "GET", "/api/supplier/contacts", cookie=cookie)[0] == 403

    raw = _token_from_mail()
    _req(port, "POST", "/api/auth/verify-email", {"token": raw})
    status, me, _ = _req(port, "GET", "/api/auth/me", cookie=cookie)
    assert status == 200
    assert me["user"]["email_verified"] is True
    assert me["user"]["account_state"] == "PENDING_SUPPLIER_APPROVAL"
    assert _req(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 403

    admin_status, _, admin_sc = _req(port, "POST", "/api/auth/login", {
        "email": "onboard-admin@corridoriq.com", "password": "OnboardAdmin123",
    })
    assert admin_status == 200
    admin_cookie = _cookie(admin_sc)
    # Approval from a contractor cookie must fail.
    assert _req(port, "POST", f"/api/admin/users/{user['id']}/approve-supplier",
                {}, cookie=cookie)[0] == 403
    status, approved, _ = _req(port, "POST", f"/api/admin/users/{user['id']}/approve-supplier",
                               {}, cookie=admin_cookie)
    assert status == 200, approved
    assert approved["account_state"] == "ACTIVE"
    assert _req(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 200
    assert _req(port, "POST", "/api/supplier/quotes", {"note": "ok"}, cookie=cookie)[0] == 201


def test_supplier_never_auto_approved(http_server):
    _, user = _register_supplier(http_server["port"], "wait@example.test")
    assert user["account_state"] != "ACTIVE"


# ---------------------------------------------------------------------------
# 10. Administrator role isolation
# ---------------------------------------------------------------------------

def test_administrator_role_isolation(http_server):
    port = http_server["port"]
    status, data, _ = _req(port, "POST", "/api/auth/register/contractor", {
        "name": "Pat", "business_name": "Pat Co", "email": "pat@example.test",
        "password": "SecurePass123", "role": "admin", "roles": ["admin"],
        "account_kind": "employee",
    })
    assert status == 201
    assert "admin" not in data["user"]["roles"]
    assert data["user"]["account_kind"] == "contractor"
    cookie = None
    # Re-login as the new contractor to confirm isolation.
    _, _, sc = _req(port, "POST", "/api/auth/login", {
        "email": "pat@example.test", "password": "SecurePass123",
    })
    cookie = _cookie(sc)
    assert _req(port, "GET", "/api/admin/users", cookie=cookie)[0] == 403
    assert _req(port, "POST", "/api/admin/users", {
        "email": "evil@example.test", "roles": ["admin"],
    }, cookie=cookie)[0] == 403


# ---------------------------------------------------------------------------
# 11–13. Password reset, token expiration, session expiration
# ---------------------------------------------------------------------------

def test_password_reset(http_server):
    port = http_server["port"]
    cookie, _ = _register_contractor(port, "resetme@example.test")
    mailer.clear_outbox()
    status, ack, _ = _req(port, "POST", "/api/auth/forgot-password", {
        "email": "resetme@example.test",
    })
    assert status == 200
    assert "if an account exists" in ack["message"].lower()
    raw = _token_from_mail("password_reset")
    status, _, _ = _req(port, "POST", "/api/auth/reset-password", {
        "token": raw, "password": "BrandNewPass123",
    })
    assert status == 200
    # Old session is dead.
    assert _req(port, "GET", "/api/auth/me", cookie=cookie)[0] == 401
    status, data, _ = _req(port, "POST", "/api/auth/login", {
        "email": "resetme@example.test", "password": "BrandNewPass123",
    })
    assert status == 200
    # Unknown email still looks the same.
    mailer.clear_outbox()
    status2, ack2, _ = _req(port, "POST", "/api/auth/forgot-password", {
        "email": "nobody@example.test",
    })
    assert status2 == 200
    assert ack2["message"] == ack["message"]


def test_verification_token_expiration(http_server):
    port = http_server["port"]
    cookie, user = _register_contractor(port, "expired@example.test")
    raw = _token_from_mail()
    conn = http_server["factory"]()
    conn.execute(
        "UPDATE auth_tokens SET expires_at=? WHERE token_hash=?",
        (_iso(_now() - timedelta(hours=1)), tokens.hash_token(raw)),
    )
    conn.commit()
    conn.close()
    status, data, _ = _req(port, "POST", "/api/auth/verify-email", {"token": raw})
    assert status == 400
    assert "expired" in data["error"].lower() or "invalid" in data["error"].lower()
    assert _req(port, "POST", "/api/contractor/rfq", {}, cookie=cookie)[0] == 403


def test_session_expiration(http_server):
    port = http_server["port"]
    cookie, user = _register_contractor(port, "session@example.test")
    token = cookie.split("=", 1)[1]
    conn = http_server["factory"]()
    conn.execute(
        "UPDATE sessions SET expires_at=? WHERE token=?",
        (_iso(_now() - timedelta(minutes=1)), token),
    )
    conn.commit()
    conn.close()
    assert _req(port, "GET", "/api/auth/me", cookie=cookie)[0] == 401


# ---------------------------------------------------------------------------
# 14. Rate limiting
# ---------------------------------------------------------------------------

def test_rate_limiting(http_server, monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "AUTH_REGISTER_PER_HOUR", 2)
    rate_limit.LIMITER.reset()
    port = http_server["port"]
    body = {
        "name": "Rate", "business_name": "Rate Co",
        "password": "SecurePass123",
    }
    assert _req(port, "POST", "/api/auth/register/contractor",
                {**body, "email": "r1@example.test"})[0] == 201
    assert _req(port, "POST", "/api/auth/register/contractor",
                {**body, "email": "r2@example.test"})[0] == 201
    status, data, _ = _req(port, "POST", "/api/auth/register/contractor",
                           {**body, "email": "r3@example.test"})
    assert status == 429
    assert "too many" in data["error"].lower()


# ---------------------------------------------------------------------------
# 15. Existing employee accounts keep working
# ---------------------------------------------------------------------------

def test_existing_employee_account_compatibility(http_server):
    port = http_server["port"]
    status, data, sc = _req(port, "POST", "/api/auth/login", {
        "email": "legacy@corridoriq.com", "password": "LegacyPass123",
    })
    assert status == 200
    assert data["user"]["account_kind"] == "employee"
    assert data["user"]["email_verified"] is True
    assert data["user"]["default_landing_page"] == "sales-dashboard.html"
    cookie = _cookie(sc)
    assert _req(port, "GET", "/api/sales/dashboard", cookie=cookie)[0] == 200
    assert _req(port, "GET", "/api/admin/users", cookie=cookie)[0] == 403
    status, admin, admin_sc = _req(port, "POST", "/api/auth/login", {
        "email": "onboard-admin@corridoriq.com", "password": "OnboardAdmin123",
    })
    assert status == 200
    assert admin["user"]["default_landing_page"] == "admin-dashboard.html"
    assert _req(port, "GET", "/api/admin/users", cookie=_cookie(admin_sc))[0] == 200


def test_admin_cannot_mint_contractor_via_employee_api(http_server):
    port = http_server["port"]
    _, _, sc = _req(port, "POST", "/api/auth/login", {
        "email": "onboard-admin@corridoriq.com", "password": "OnboardAdmin123",
    })
    status, data, _ = _req(port, "POST", "/api/admin/users", {
        "email": "via-admin@example.test", "roles": ["contractor"],
    }, cookie=_cookie(sc))
    assert status == 400
    assert "self-registration" in data["error"].lower()


# ---------------------------------------------------------------------------
# 16. Mobile navigation / form affordances
# ---------------------------------------------------------------------------

def test_mobile_navigation_and_forms():
    css = (REPO / "site.css").read_text(encoding="utf-8")
    assert "min-height: 44px" in css
    assert "site-nav.open .site-cta" in css
    assert "position: static" in css
    js = (REPO / "site.js").read_text(encoding="utf-8")
    assert 'aria-expanded' in js
    for name in ("register-contractor.html", "register-supplier.html",
                 "login.html", "reset-password.html", "verify-email.html"):
        html = (REPO / name).read_text(encoding="utf-8")
        assert 'name="viewport"' in html
        assert "width=device-width" in html
        assert 'autocomplete=' in html


def test_public_preview_static_is_served():
    from pipeline.api.server import static_target
    for path in ("/explore.html", "/register-contractor.html",
                 "/register-supplier.html", "/contractor-welcome.html",
                 "/supplier-welcome.html", "/verify-email.html",
                 "/reset-password.html", "/auth-public.css", "/auth-public.js"):
        target, ctype = static_target(path)
        assert target is not None, path
        assert ctype


# ---------------------------------------------------------------------------
# Negative authorization extras
# ---------------------------------------------------------------------------

def test_contractor_cannot_use_supplier_live_routes(http_server):
    port = http_server["port"]
    cookie, _ = _register_contractor(port, "nosup@example.test")
    raw = _token_from_mail()
    _req(port, "POST", "/api/auth/verify-email", {"token": raw})
    assert _req(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 403
    assert _req(port, "GET", "/api/supplier/dashboard", cookie=cookie)[0] == 403


def test_passwords_and_tokens_are_hashed(http_server):
    port = http_server["port"]
    _register_contractor(port, "hashed@example.test")
    raw = _token_from_mail()
    conn = http_server["factory"]()
    row = conn.execute("SELECT password_hash FROM users WHERE normalized_email=?",
                       ("hashed@example.test",)).fetchone()
    assert row["password_hash"]
    assert "SecurePass123" not in row["password_hash"]
    token_row = conn.execute("SELECT token_hash FROM auth_tokens").fetchone()
    assert token_row["token_hash"] != raw
    assert tokens.hash_token(raw) == token_row["token_hash"]
    conn.close()


def test_direct_onboarding_helpers_on_memory_db(db):
    token, user = __import__("pipeline.auth.onboarding", fromlist=["register_contractor"]).register_contractor(
        db, {"name": "Sam", "business_name": "Sam LLC",
             "email": "sam@example.test", "password": "SecurePass123"},
    )
    assert token and user["account_kind"] == "contractor"
    with pytest.raises(auth.AuthError):
        __import__("pipeline.auth.onboarding", fromlist=["register_contractor"]).register_contractor(
            db, {"name": "Sam", "business_name": "Sam LLC",
                 "email": "sam@example.test", "password": "SecurePass123"},
        )
