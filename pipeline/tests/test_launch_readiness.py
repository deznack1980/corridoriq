"""Phase 2 launch validation: SEO, mail safety, workflow demo, and gates."""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from pipeline.api import seo
from pipeline.auth import mailer, rate_limit
from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import PROJECT_ROOT, SCHEMA_PATH

import pytest

REPO = Path(PROJECT_ROOT)
PRIVATE_HINTS = (
    "/api/",
    "admin-dashboard",
    "sales-dashboard",
    "contractor-welcome",
    "supplier-welcome",
    "verify-email",
    "reset-password",
    "user-management",
)


@pytest.fixture()
def http_server(tmp_path, monkeypatch):
    import pipeline.api.server as server_mod

    db_file = tmp_path / "launch.db"
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


def _raw(port, method, path, body=None, cookie=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if cookie:
        headers["Cookie"] = cookie
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    resp = conn.getresponse()
    raw = resp.read()
    headers_out = {k.lower(): v for k, v in resp.getheaders()}
    conn.close()
    return resp.status, raw, headers_out


def _req(port, method, path, body=None, cookie=None):
    status, raw, headers = _raw(port, method, path, body, cookie)
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {"_raw": raw.decode("utf-8", "replace")}
    return status, data, headers.get("set-cookie")


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
# SEO
# ---------------------------------------------------------------------------

def test_robots_and_sitemap_http(http_server):
    port = http_server["port"]
    status, raw, headers = _raw(port, "GET", "/robots.txt")
    assert status == 200
    text = raw.decode("utf-8")
    assert headers["content-type"].startswith("text/plain")
    assert "User-agent: *" in text
    assert "Disallow: /api/" in text
    assert "Sitemap: https://corridoriq.pro/sitemap.xml" in text
    for hint in PRIVATE_HINTS:
        assert hint in text

    status, raw, headers = _raw(port, "GET", "/sitemap.xml")
    assert status == 200
    xml = raw.decode("utf-8")
    assert headers["content-type"].startswith("application/xml")
    assert "https://corridoriq.pro/" in xml
    assert "https://corridoriq.pro/explore.html" in xml
    assert "https://corridoriq.pro/for-contractors.html" in xml
    assert "https://corridoriq.pro/for-suppliers.html" in xml
    assert "https://corridoriq.pro/register-contractor.html" in xml
    assert "https://corridoriq.pro/register-supplier.html" in xml
    assert "https://corridoriq.pro/login.html" in xml
    for hint in ("contractor-welcome", "supplier-welcome", "admin-dashboard",
                 "verify-email", "/api/", "127.0.0.1"):
        assert hint not in xml


def test_seo_module_matches_static_files():
    assert seo.PRODUCTION_ORIGIN == "https://corridoriq.pro"
    assert seo.robots_txt().strip() == (REPO / "robots.txt").read_text(encoding="utf-8").strip()
    assert seo.sitemap_xml().strip() == (REPO / "sitemap.xml").read_text(encoding="utf-8").strip()
    assert seo.sitemap_xml().count("<loc>") == len(seo.PUBLIC_PATHS)


def test_public_marketing_pages_are_indexable():
    for name in ("home.html", "explore.html", "for-contractors.html",
                 "for-suppliers.html", "register-contractor.html",
                 "register-supplier.html", "login.html"):
        html = (REPO / name).read_text(encoding="utf-8")
        assert 'name="robots" content="noindex"' not in html, name


def test_private_pages_declare_noindex():
    for name in ("contractor-welcome.html", "supplier-welcome.html",
                 "verify-email.html", "reset-password.html"):
        html = (REPO / name).read_text(encoding="utf-8")
        assert 'name="robots" content="noindex"' in html, name


# ---------------------------------------------------------------------------
# Mail safety
# ---------------------------------------------------------------------------

def test_health_exposes_mail_flags_not_secrets(http_server):
    port = http_server["port"]
    status, data, _ = _req(port, "GET", "/api/health")
    assert status == 200
    assert data["ok"] is True
    assert "configured" in data["mail"]
    assert data["mail"]["delivery_mode"] in ("smtp", "outbox")
    blob = json.dumps(data).lower()
    assert "password" not in blob
    assert "smtp_user" not in blob
    assert "token" not in blob


def test_mail_status_is_admin_only(http_server):
    port = http_server["port"]
    assert _req(port, "GET", "/api/admin/mail-status")[0] == 401
    cookie, _ = _register_contractor(port, "nomail@example.test")
    assert _req(port, "GET", "/api/admin/mail-status", cookie=cookie)[0] == 403
    _, _, sc = _req(port, "POST", "/api/auth/login", {
        "email": "legacy@corridoriq.com", "password": "LegacyPass123",
    })
    assert _req(port, "GET", "/api/admin/mail-status", cookie=_cookie(sc))[0] == 403
    _, _, admin_sc = _req(port, "POST", "/api/auth/login", {
        "email": "onboard-admin@corridoriq.com", "password": "OnboardAdmin123",
    })
    status, data, _ = _req(port, "GET", "/api/admin/mail-status", cookie=_cookie(admin_sc))
    assert status == 200
    assert "smtp_configured" in data
    assert "password" not in json.dumps(data).lower() or "password_present" in data
    assert data.get("password_present") in (True, False)
    assert "CORRIDORIQ_SMTP_PASSWORD" not in json.dumps(data)


def test_mailer_redacts_tokens_on_disk(tmp_path, monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "MAIL_OUTBOX_DIR", str(tmp_path))
    mailer.clear_outbox()
    mailer.verification_email("a@example.test", "SECRETTOKENVALUE", user_id=1)
    files = list(tmp_path.glob("*.json"))
    assert files
    disk = files[0].read_text(encoding="utf-8")
    assert "SECRETTOKENVALUE" not in disk
    assert "token=[redacted]" in disk
    assert mailer.OUTBOX[-1]["text"].count("SECRETTOKENVALUE") == 1


def test_diagnose_never_includes_credentials(monkeypatch):
    from pipeline.config import settings
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.test")
    monkeypatch.setattr(settings, "SMTP_USER", "mailer@example.test")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "super-secret-password")
    report = mailer.diagnose()
    blob = json.dumps(report)
    assert "super-secret-password" not in blob
    assert "mailer@example.test" not in blob
    assert report["smtp_configured"] is True
    assert report["delivery_mode"] == "smtp"
    assert report["host_present"] is True
    assert report["user_present"] is True
    assert report["password_present"] is True


# ---------------------------------------------------------------------------
# Contractor welcome / workflow demo
# ---------------------------------------------------------------------------

def test_contractor_welcome_distinguishes_demo_from_live(http_server):
    port = http_server["port"]
    cookie, _ = _register_contractor(port, "workflow@example.test")
    status, dash, _ = _req(port, "GET", "/api/contractor/dashboard", cookie=cookie)
    assert status == 200
    demo = dash["workflow_demo"]
    assert "not a live" in demo["label"].lower() or "demonstration" in demo["label"].lower()
    assert len(demo["bom"]) >= 3
    assert len(demo["suppliers"]) >= 2
    blob = json.dumps(dash)
    assert "$" not in blob
    assert "unit price" not in blob.lower()
    locked_keys = {item["key"] for item in dash["locked"]}
    assert "live_rfq" in locked_keys
    assert "private_quotations" in locked_keys
    html = (REPO / "contractor-welcome.html").read_text(encoding="utf-8")
    assert "Available now" in html
    assert "Demonstration" in html
    assert "Locked" in html
    assert "bill of materials" in html.lower()
    assert "$" not in html


def test_resend_verification_and_password_reset_flow(http_server):
    port = http_server["port"]
    cookie, user = _register_contractor(port, "cycle@example.test")
    mailer.clear_outbox()
    status, ack, _ = _req(port, "POST", "/api/auth/resend-verification", {
        "email": user["email"],
    })
    assert status == 200
    raw = _token_from_mail()
    assert _req(port, "POST", "/api/auth/verify-email", {"token": raw})[0] == 200
    mailer.clear_outbox()
    assert _req(port, "POST", "/api/auth/forgot-password", {
        "email": user["email"],
    })[0] == 200
    reset = _token_from_mail("password_reset")
    assert _req(port, "POST", "/api/auth/reset-password", {
        "token": reset, "password": "NewerPass123",
    })[0] == 200
    assert _req(port, "GET", "/api/auth/me", cookie=cookie)[0] == 401
    status, data, _ = _req(port, "POST", "/api/auth/login", {
        "email": user["email"], "password": "NewerPass123",
    })
    assert status == 200


# ---------------------------------------------------------------------------
# Supplier approval
# ---------------------------------------------------------------------------

def test_pending_supplier_cannot_see_rfqs_or_contacts(http_server):
    port = http_server["port"]
    cookie, user = _register_supplier(port, "hold@example.test")
    status, dash, _ = _req(port, "GET", "/api/supplier/dashboard", cookie=cookie)
    assert status == 200
    assert dash["account_state"] == "PENDING_EMAIL_VERIFICATION"
    assert any(step["state"] == "PENDING_SUPPLIER_APPROVAL" for step in dash["onboarding_path"])
    for path in ("/api/supplier/rfqs", "/api/supplier/contacts", "/api/supplier/commercial"):
        assert _req(port, "GET", path, cookie=cookie)[0] == 403
    assert _req(port, "GET", "/api/sales/companies", cookie=cookie)[0] == 403
    assert _req(port, "GET", "/api/admin/users", cookie=cookie)[0] == 403

    raw = _token_from_mail()
    _req(port, "POST", "/api/auth/verify-email", {"token": raw})
    status, me, _ = _req(port, "GET", "/api/auth/me", cookie=cookie)
    assert me["user"]["account_state"] == "PENDING_SUPPLIER_APPROVAL"
    assert _req(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 403
    assert _req(port, "GET", "/api/supplier/contacts", cookie=cookie)[0] == 403

    _, _, admin_sc = _req(port, "POST", "/api/auth/login", {
        "email": "onboard-admin@corridoriq.com", "password": "OnboardAdmin123",
    })
    status, approved, _ = _req(
        port, "POST", f"/api/admin/users/{user['id']}/approve-supplier",
        {}, cookie=_cookie(admin_sc),
    )
    assert status == 200
    assert approved["account_state"] == "ACTIVE"
    assert _req(port, "GET", "/api/supplier/rfqs", cookie=cookie)[0] == 200
    assert _req(port, "GET", "/api/supplier/contacts", cookie=cookie)[0] == 200
    assert _req(port, "GET", "/api/sales/dashboard", cookie=cookie)[0] == 403


# ---------------------------------------------------------------------------
# Session security + employee compatibility + public pages
# ---------------------------------------------------------------------------

def test_session_cookie_flags(http_server):
    port = http_server["port"]
    status, _, set_cookie = _req(port, "POST", "/api/auth/login", {
        "email": "legacy@corridoriq.com", "password": "LegacyPass123",
    })
    assert status == 200
    lowered = set_cookie.lower()
    assert "httponly" in lowered
    assert "samesite=lax" in lowered
    assert "path=/" in lowered


def test_employee_and_admin_still_land_correctly(http_server):
    port = http_server["port"]
    status, data, sc = _req(port, "POST", "/api/auth/login", {
        "email": "legacy@corridoriq.com", "password": "LegacyPass123",
    })
    assert status == 200
    assert data["user"]["default_landing_page"] == "sales-dashboard.html"
    assert _req(port, "GET", "/api/sales/dashboard", cookie=_cookie(sc))[0] == 200
    status, admin, admin_sc = _req(port, "POST", "/api/auth/login", {
        "email": "onboard-admin@corridoriq.com", "password": "OnboardAdmin123",
    })
    assert status == 200
    assert admin["user"]["default_landing_page"] == "admin-dashboard.html"
    assert _req(port, "GET", "/api/admin/users", cookie=_cookie(admin_sc))[0] == 200


def test_public_pages_and_mobile_nav_remain_accessible():
    from pipeline.api.server import static_target
    for path in (
        "/", "/explore.html", "/for-contractors.html", "/for-suppliers.html",
        "/register-contractor.html", "/register-supplier.html", "/login.html",
        "/robots.txt", "/sitemap.xml",
    ):
        target, ctype = static_target(path)
        assert target is not None, path
        assert ctype
    css = (REPO / "site.css").read_text(encoding="utf-8")
    assert "min-height: 44px" in css
    assert "site-nav.open" in css
    welcome_css = (REPO / "auth-public.css").read_text(encoding="utf-8")
    assert ".status-row" in welcome_css
    assert "grid-template-columns: 1fr" in welcome_css
