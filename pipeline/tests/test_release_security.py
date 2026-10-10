"""Release-candidate security: proxy IP trust, isolation, mail, schema."""

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

from pipeline.api import seo
from pipeline.auth import client_ip, mailer, rate_limit, tokens
from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import PROJECT_ROOT, SCHEMA_PATH
from pipeline.db.database import migrate_schema

REPO = Path(PROJECT_ROOT)


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    import pipeline.api.server as server_mod

    db_file = tmp_path / "sec.db"
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
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    yield {"port": port, "factory": factory}
    srv.shutdown()
    mailer.clear_outbox()
    rate_limit.LIMITER.reset()


def _raw(port, method, path, body=None, cookie=None, extra_headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    if extra_headers:
        headers.update(extra_headers)
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


def _register_contractor(port, email, extra_headers=None):
    status, data, sc = _raw(port, "POST", "/api/auth/register/contractor", {
        "name": "Alex Rivera", "business_name": "Rivera Plumbing",
        "email": email, "password": "SecurePass123",
    }, extra_headers=extra_headers)
    return status, data, _cookie(sc)


def _register_supplier(port, email):
    status, data, sc = _raw(port, "POST", "/api/auth/register/supplier", {
        "name": "Jordan Lee", "business_name": "Valley Pipe Supply",
        "email": email, "password": "SecurePass123",
        "phone": "480-555-0199", "business_category": "plumbing",
    })
    assert status == 201, data
    return _cookie(sc), data["user"]


# ---------------------------------------------------------------------------
# Task 1 — client IP / rate limits
# ---------------------------------------------------------------------------

def test_trust_proxy_defaults_off():
    from pipeline.config import settings
    assert settings.TRUST_PROXY is False
    assert (settings.TRUSTED_PROXIES or "") == ""


def test_forwarded_for_is_ignored_without_trust(monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "TRUST_PROXY", False)
    ip = client_ip.resolve("203.0.113.9", {"X-Forwarded-For": "198.51.100.10"})
    assert ip == "203.0.113.9"


def test_forwarded_for_used_only_from_trusted_peer(monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "loopback")
    spoofed = client_ip.resolve("198.51.100.20", {"X-Forwarded-For": "203.0.113.8"})
    assert spoofed == "198.51.100.20"
    trusted = client_ip.resolve("127.0.0.1", {"X-Forwarded-For": "203.0.113.8"})
    assert trusted == "203.0.113.8"


def test_cloudflare_connecting_ip_preferred(monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "private")
    ip = client_ip.resolve("10.1.2.3", {
        "X-Forwarded-For": "198.51.100.1, 10.1.2.3",
        "CF-Connecting-IP": "203.0.113.44",
    })
    assert ip == "203.0.113.44"


def test_spoofed_xff_cannot_bypass_shared_ip_limit(http_server, monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "TRUST_PROXY", False)
    monkeypatch.setattr(settings, "AUTH_REGISTER_PER_HOUR", 2)
    rate_limit.LIMITER.reset()
    port = http_server["port"]
    assert _register_contractor(port, "a@example.test",
                                {"X-Forwarded-For": "203.0.113.1"})[0] == 201
    assert _register_contractor(port, "b@example.test",
                                {"X-Forwarded-For": "203.0.113.2"})[0] == 201
    status, data, _ = _register_contractor(port, "c@example.test",
                                           {"X-Forwarded-For": "203.0.113.3"})
    assert status == 429
    assert "too many" in data["error"].lower()


def test_trusted_proxy_separates_real_clients(http_server, monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "TRUST_PROXY", True)
    monkeypatch.setattr(settings, "TRUSTED_PROXIES", "loopback")
    monkeypatch.setattr(settings, "AUTH_REGISTER_PER_HOUR", 1)
    rate_limit.LIMITER.reset()
    port = http_server["port"]
    assert _register_contractor(port, "one@example.test",
                                {"X-Forwarded-For": "203.0.113.10"})[0] == 201
    status, _, _ = _register_contractor(port, "two@example.test",
                                        {"X-Forwarded-For": "203.0.113.10"})
    assert status == 429
    assert _register_contractor(port, "three@example.test",
                                {"X-Forwarded-For": "203.0.113.11"})[0] == 201


def test_account_identity_rate_limit_is_independent(monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "AUTH_LOGIN_PER_MINUTE", 2)
    rate_limit.LIMITER.reset()
    rate_limit.check("login", "203.0.113.1", "same@example.test")
    rate_limit.check("login", "203.0.113.2", "same@example.test")
    with pytest.raises(auth.AuthError) as exc:
        rate_limit.check("login", "203.0.113.3", "same@example.test")
    assert exc.value.status == 429


# ---------------------------------------------------------------------------
# Task 2 — authorization matrix
# ---------------------------------------------------------------------------

def test_anonymous_and_unverified_contractor_isolation(http_server):
    port = http_server["port"]
    assert _raw(port, "GET", "/api/sales/dashboard")[0] == 401
    assert _raw(port, "GET", "/api/contractor/intelligence")[0] == 401
    status, data, cookie = _register_contractor(port, "gate@example.test")
    assert status == 201
    assert _raw(port, "POST", "/api/contractor/rfq", {"title": "x"}, cookie=cookie)[0] == 403
    assert _raw(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 403
    assert _raw(port, "GET", "/api/sales/dashboard", cookie=cookie)[0] == 403
    assert _raw(port, "GET", "/api/admin/users", cookie=cookie)[0] == 403


def test_verified_contractor_cannot_use_supplier_or_crm(http_server):
    port = http_server["port"]
    _, _, cookie = _register_contractor(port, "verified@example.test")
    _raw(port, "POST", "/api/auth/verify-email", {"token": _token_from_mail()})
    assert _raw(port, "POST", "/api/contractor/rfq", {"title": "ok"}, cookie=cookie)[0] == 201
    assert _raw(port, "GET", "/api/supplier/contacts", cookie=cookie)[0] == 403
    assert _raw(port, "GET", "/api/admin/mail-status", cookie=cookie)[0] == 403


def test_supplier_states_and_employee_access(http_server):
    port = http_server["port"]
    cookie, user = _register_supplier(port, "hold@example.test")
    assert _raw(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 403
    _raw(port, "POST", "/api/auth/verify-email", {"token": _token_from_mail()})
    assert _raw(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 403
    _, _, admin_sc = _raw(port, "POST", "/api/auth/login", {
        "email": "onboard-admin@corridoriq.com", "password": "OnboardAdmin123",
    })
    admin = _cookie(admin_sc)
    assert _raw(port, "POST", f"/api/admin/users/{user['id']}/approve-supplier",
                {}, cookie=admin)[0] == 200
    assert _raw(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 200
    assert _raw(port, "GET", "/api/sales/dashboard", cookie=cookie)[0] == 403
    assert _raw(port, "GET", "/api/admin/users", cookie=cookie)[0] == 403
    assert _raw(port, "POST", f"/api/admin/users/{user['id']}/suspend",
                {}, cookie=admin)[0] == 200
    assert _raw(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 401
    login_status, _, _ = _raw(port, "POST", "/api/auth/login", {
        "email": "hold@example.test", "password": "SecurePass123",
    })
    assert login_status in (401, 403)
    _, _, emp_sc = _raw(port, "POST", "/api/auth/login", {
        "email": "legacy@corridoriq.com", "password": "LegacyPass123",
    })
    assert _raw(port, "GET", "/api/sales/dashboard", cookie=_cookie(emp_sc))[0] == 200


def test_password_reset_does_not_restore_suspended_or_grant_admin(http_server):
    port = http_server["port"]
    cookie, user = _register_supplier(port, "reset-suspend@example.test")
    raw_verify = _token_from_mail()
    _raw(port, "POST", "/api/auth/verify-email", {"token": raw_verify})
    mailer.clear_outbox()
    _raw(port, "POST", "/api/auth/forgot-password", {"email": "reset-suspend@example.test"})
    reset_token = _token_from_mail("password_reset")
    _, _, admin_sc = _raw(port, "POST", "/api/auth/login", {
        "email": "onboard-admin@corridoriq.com", "password": "OnboardAdmin123",
    })
    _raw(port, "POST", f"/api/admin/users/{user['id']}/suspend",
         {}, cookie=_cookie(admin_sc))
    status, _, _ = _raw(port, "POST", "/api/auth/reset-password", {
        "token": reset_token, "password": "BrandNewPass123",
    })
    assert status == 400
    assert _raw(port, "GET", "/api/auth/me", cookie=cookie)[0] == 401
    status, data, sc = _raw(port, "POST", "/api/auth/login", {
        "email": "reset-suspend@example.test", "password": "BrandNewPass123",
    })
    assert status != 200
    if sc:
        assert _raw(port, "GET", "/api/admin/users", cookie=_cookie(sc))[0] != 200


# ---------------------------------------------------------------------------
# Task 3 — mail readiness (no real SMTP)
# ---------------------------------------------------------------------------

def test_mail_diagnose_and_single_use_tokens(http_server):
    report = mailer.diagnose()
    assert "smtp_configured" in report
    assert "password" not in json.dumps(report) or "password_present" in report
    assert report.get("password_present") in (True, False)
    port = http_server["port"]
    _register_contractor(port, "mail@example.test")
    raw = _token_from_mail()
    assert _raw(port, "POST", "/api/auth/verify-email", {"token": raw})[0] == 200
    assert _raw(port, "POST", "/api/auth/verify-email", {"token": raw})[0] == 400
    disk = json.dumps(mailer.diagnose()).lower()
    assert "corridoriq_smtp_password" not in disk


# ---------------------------------------------------------------------------
# Task 4 — additive schema
# ---------------------------------------------------------------------------

def test_onboarding_columns_are_additive_and_preserve_accounts(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE users (
            id INTEGER PRIMARY KEY, organization_id INTEGER, email TEXT,
            normalized_email TEXT UNIQUE, password_hash TEXT,
            is_active INTEGER DEFAULT 1, must_change_password INTEGER DEFAULT 0,
            failed_login_count INTEGER DEFAULT 0, locked_until TEXT,
            last_login_at TEXT, created_at TEXT, updated_at TEXT
        )"""
    )
    conn.execute(
        "INSERT INTO users (organization_id, email, normalized_email, password_hash, "
        "created_at, updated_at) VALUES (1,?,?,?,'2024-01-01','2024-01-01')",
        ("legacy@corridoriq.com", "legacy@corridoriq.com", "hashed-not-plain"),
    )
    conn.commit()
    migrate_schema(conn)
    migrate_schema(conn)
    row = conn.execute("SELECT * FROM users WHERE email=?",
                       ("legacy@corridoriq.com",)).fetchone()
    assert row["password_hash"] == "hashed-not-plain"
    assert row["account_kind"] == "employee"
    assert row["account_state"] == "ACTIVE"
    assert row["email_verified_at"] is None
    conn.close()


# ---------------------------------------------------------------------------
# Task 6 — SEO still served
# ---------------------------------------------------------------------------

def test_robots_and_sitemap_still_public(http_server):
    port = http_server["port"]
    status, data, _ = _raw(port, "GET", "/robots.txt")
    assert status == 200
    text = data.get("_raw", "")
    assert "Disallow: /api/" in text
    assert seo.PRODUCTION_ORIGIN in text
    status, data, _ = _raw(port, "GET", "/sitemap.xml")
    assert status == 200
    xml = data.get("_raw", "")
    assert "https://corridoriq.pro/" in xml
    assert "contractor-welcome" not in xml
    assert "127.0.0.1" not in xml
