"""Phase 2 security-review remediations and Claude adversarial regressions."""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.api import server as server_mod
from pipeline.auth import mail, readiness, sessions, throttle, tokens
from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.billing import contractor_accounts as ca
from pipeline.config.settings import SCHEMA_PATH
from pipeline.tests.test_billing import _login, _req
from pipeline.tests.test_contractor_pilot import PW, _sink_token, client, signup

REPO = Path(__file__).resolve().parents[2]
J = {"Content-Type": "application/json"}


def _copied_env(tmp_path, monkeypatch):
    from pipeline.pilot import accounts
    from pipeline.pilot.platform import ENV_ROOT, Platform
    root = tmp_path / "pilot"
    monkeypatch.setenv(ENV_ROOT, str(root))
    billing_db = tmp_path / "billing.db"
    init = sqlite3.connect(billing_db)
    init.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    init.row_factory = sqlite3.Row
    seed_auth(init)
    init.close()

    def factory():
        conn = sqlite3.connect(billing_db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    monkeypatch.setattr(server_mod, "_factory", factory)
    mail.sink().clear()
    throttle.pilot.clear()
    throttle.portal.clear()
    platform = Platform(root)
    conn = platform.connect()
    accounts.create_supplier(platform, "supplier-a", "Synthetic Supply A")
    code_a = accounts.create_referral_code(conn, platform, "supplier-a", "supa", "counter")
    accounts.add_supplier_user(conn, platform, "supplier-a", "inbox-a@example.test", PW, "Inbox A")
    conn.close()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield {"root": root, "platform": platform, "port": srv.server_address[1],
               "code_a": code_a, "billing_db": billing_db, "factory": factory}
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    yield from _copied_env(tmp_path, monkeypatch)


def _portal(tmp_path, monkeypatch):
    db_path = tmp_path / "portal.db"

    def factory():
        c = sqlite3.connect(db_path)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(c)
    org = c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    auth.create_user(c, organization_id=org, email="reset-main@example.test",
                     password="Passw0rd!x", role_names=["admin"], must_change_password=False)
    c.close()
    mail.sink().clear()
    throttle.portal.clear()
    monkeypatch.setattr(server_mod, "_factory", factory)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, factory


def test_invitation_fails_after_forgot_reset_login(tmp_path, monkeypatch):
    """Claude attack: invite → forgot/reset → login → replay old invitation."""
    srv, factory = _portal(tmp_path, monkeypatch)
    try:
        conn = factory()
        operator = auth.build_user_context(conn, conn.execute(
            "SELECT * FROM users WHERE normalized_email='reset-main@example.test'").fetchone())
        ca.create_contractor_account(conn, operator, {
            "company_name": "Replay Co", "contact_name": "Owner Person",
            "email": "replay@example.test", "business_zip": "85004",
        })
        invite = mail.sink().outbox(to="replay@example.test", kind=mail.PORTAL_INVITE)[-1].link.split("#t=", 1)[1]
        assert auth.request_password_reset(conn, "replay@example.test") == {"status": "check_email"}
        reset = mail.sink().outbox(to="replay@example.test", kind=mail.PORTAL_RESET)[-1].link.split("#t=", 1)[1]
        auth.complete_password_reset(conn, reset, "Reset-Pass-123")
        token, user = auth.login(conn, "replay@example.test", "Reset-Pass-123")
        assert user["email"] == "replay@example.test"
        with pytest.raises(auth.AuthError):
            ca.accept_owner_invitation(conn, invite, "Takeover-Pass-123")
        assert auth.get_current_user(conn, token) is not None
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_invitation_fails_after_authenticated_password_change(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(conn)
    now = "2026-10-05T12:00:00+00:00"
    org = conn.execute(
        "INSERT INTO organizations (name, slug, is_active, account_type, created_at, updated_at) "
        "VALUES ('Own','own-co',1,'contractor',?,?)", (now, now)).lastrowid
    uid = auth.create_user(conn, organization_id=org, email="owner@example.test",
                           password="Old-Pass-123", role_names=["contractor_owner"],
                           must_change_password=False)
    row = conn.execute("SELECT password_hash FROM users WHERE id=?", (uid,)).fetchone()
    raw, _ = tokens.issue(conn, purpose=tokens.PORTAL_INVITE, ttl=tokens.INVITE_TTL,
                          user_id=uid, email="owner@example.test",
                          password_marker=tokens.password_marker(row["password_hash"]),
                          context={"organization_id": org})
    conn.commit()
    mail.sink().clear()
    auth.change_password(conn, uid, "Old-Pass-123", "Secured-Pass-123")
    with pytest.raises(auth.AuthError):
        ca.accept_owner_invitation(conn, raw, "Hijack-Pass-123")
    token, _ = auth.login(conn, "owner@example.test", "Secured-Pass-123")
    assert token
    assert mail.sink().outbox(to="owner@example.test", kind=mail.PORTAL_PASSWORD_CHANGED)


def test_invitation_acceptance_revokes_existing_sessions(tmp_path, monkeypatch):
    srv, factory = _portal(tmp_path, monkeypatch)
    try:
        conn = factory()
        operator = auth.build_user_context(conn, conn.execute(
            "SELECT * FROM users WHERE normalized_email='reset-main@example.test'").fetchone())
        res = ca.create_contractor_account(conn, operator, {
            "company_name": "Session Co", "contact_name": "Owner Person",
            "email": "sess@example.test", "business_zip": "85004",
        })
        stale = sessions.create_session(conn, res["owner"]["id"])
        assert auth.get_current_user(conn, stale) is not None
        invite = mail.sink().outbox(to="sess@example.test", kind=mail.PORTAL_INVITE)[-1].link.split("#t=", 1)[1]
        ca.accept_owner_invitation(conn, invite, "Owner-Chosen-Pass-123")
        assert auth.get_current_user(conn, stale) is None
        assert mail.sink().outbox(to="sess@example.test", kind=mail.PORTAL_PASSWORD_CHANGED)
        conn.close()
    finally:
        srv.shutdown()
        srv.server_close()


def test_mail_failure_does_not_enumerate_portal_or_pilot(env, monkeypatch):
    monkeypatch.setenv(mail.ENV_PROVIDER, "postmark")
    known = client(env).call("POST", "/api/pilot/auth/signup", {
        "name": "Pat", "business_name": "Pat Pipe", "email": "exists@example.test",
    })
    missing = client(env).call("POST", "/api/pilot/auth/signup", {
        "name": "Pat", "business_name": "Pat Pipe", "email": "missing@example.test",
    })
    assert known == missing == (202, {"status": "check_email"})
    port = env["port"]
    s1, _, r1 = _req(port, "POST", "/api/auth/forgot", {"email": "nobody@example.test"}, J)
    s2, _, r2 = _req(port, "POST", "/api/auth/forgot", {"email": "inbox-a@example.test"}, J)
    assert (s1, json.loads(r1)) == (s2, json.loads(r2)) == (202, {"status": "check_email"})


def test_successful_logins_do_not_exhaust_failure_throttle(env):
    throttle.pilot.clear()
    c, _ = signup(env, "ok@example.test", "OK Co")
    for _ in range(25):
        status, _ = client(env).call("POST", "/api/pilot/auth/login",
                                     {"email": "ok@example.test", "password": PW})
        assert status == 200
    assert client(env).call("POST", "/api/pilot/auth/login",
                            {"email": "ok@example.test", "password": PW})[0] == 200
    for _ in range(30):
        client(env).call("POST", "/api/pilot/auth/login",
                         {"email": "ok@example.test", "password": "wrong-pass"})
    assert client(env).call("POST", "/api/pilot/auth/login",
                            {"email": "ok@example.test", "password": PW})[0] == 429


def test_xff_cannot_bypass_and_missing_production_proxy_fails_health(env, monkeypatch):
    from pipeline.api.client_address import resolve_client_ip
    assert resolve_client_ip("127.0.0.1", {"X-Forwarded-For": "198.51.100.9"}) == "127.0.0.1"
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", True)
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.delenv("CORRIDORIQ_TRUSTED_PROXY_SECRET", raising=False)
    s, _, raw = _req(env["port"], "GET", "/api/health")
    body = json.loads(raw)
    assert s == 503 and body["ok"] is False
    assert "trusted_proxy_secret_missing" in body["problems"]
    assert "production_mail_sink" in body["problems"]
    assert "public_base_url_not_https" in body["problems"]
    dumped = json.dumps(body)
    assert "CORRIDORIQ_TRUSTED_PROXY_SECRET" not in dumped
    with pytest.raises(SystemExit):
        readiness.refuse_unsafe_production()


def test_production_sink_and_unsupported_provider_fail_readiness(monkeypatch):
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", True)
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.setenv("CORRIDORIQ_TRUSTED_PROXY_SECRET", "x" * 32)
    monkeypatch.setenv(mail.ENV_PUBLIC_BASE_URL, "https://app.corridoriq.pro")
    monkeypatch.delenv(mail.ENV_PROVIDER, raising=False)
    problems = readiness.production_problems()
    assert "production_mail_sink" in problems
    monkeypatch.setenv(mail.ENV_PROVIDER, "postmark")
    problems = readiness.production_problems()
    assert "production_mail_provider_unsupported" in problems
    with pytest.raises(mail.MailNotConfigured):
        mail.get_mailer()
    monkeypatch.delenv("CORRIDORIQ_ENV")
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", False)
    assert readiness.production_problems() == []


def test_production_cookies_are_secure_for_portal_and_pilot(env, monkeypatch):
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", True)
    c, _ = signup(env, "secure@example.test", "Secure Co", code=env["code_a"])
    assert "Secure" in c.last_pilot_cookie
    port = env["port"]
    conn = env["factory"]()
    org = conn.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()
    auth.create_user(conn, organization_id=org["id"], email="portal-secure@example.test",
                     password="Passw0rd!x", role_names=["admin"], must_change_password=False)
    conn.close()
    s, headers, _ = _req(port, "POST", "/api/auth/login",
                         {"email": "portal-secure@example.test", "password": "Passw0rd!x"}, J)
    assert s == 200 and "Secure" in headers.get("set-cookie", "")
    inbox = client(env)
    status, _ = inbox.call("POST", "/api/pilot/auth/login",
                           {"email": "inbox-a@example.test", "password": PW})
    assert status == 200 and "Secure" in inbox.last_pilot_cookie


def test_text_plain_portal_login_is_rejected(env):
    for path in ("/api/auth/login", "/api/auth/forgot", "/api/auth/reset",
                 "/api/auth/accept-invite"):
        s, _, raw = _req(env["port"], "POST", path,
                         {"email": "x@example.test", "password": "abcdefgh"},
                         {"Content-Type": "text/plain"})
        assert s == 415 and json.loads(raw)["error"] == "json_required", path


def test_malformed_inspect_is_400_not_500(env):
    c = client(env)
    assert c.call("POST", "/api/pilot/auth/inspect", {"token": ["list"], "purpose": "pilot_signup"})[0] == 400
    assert c.call("POST", "/api/pilot/auth/inspect", {"token": "abc", "purpose": ["x"]})[0] == 400
    assert c.call("POST", "/api/pilot/auth/inspect", {"token": "abc", "purpose": "not-a-purpose"})[0] == 400
    assert c.call("POST", "/api/pilot/auth/inspect", {"token": "abc", "purpose": "pilot_signup"}) == (
        200, {"valid": False})


def test_email_change_revokes_sessions_and_uses_generic_duplicate_error():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(conn)
    org = conn.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    uid = auth.create_user(conn, organization_id=org, email="old@example.test",
                           password=PW, role_names=["admin"], must_change_password=False)
    other = auth.create_user(conn, organization_id=org, email="taken@example.test",
                             password=PW, role_names=["admin"], must_change_password=False)
    token = sessions.create_session(conn, uid)
    auth.mark_email_verified(conn, uid, "old@example.test")
    with pytest.raises(ValueError, match="email cannot be updated"):
        auth.apply_email_change(conn, uid, "taken@example.test")
    auth.apply_email_change(conn, uid, "new@example.test")
    assert auth.get_current_user(conn, token) is None
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    assert row["normalized_email"] == "new@example.test" and not auth.email_is_verified(row)
    assert other


def test_pilot_reset_distinguishes_weak_password(env):
    c, _ = signup(env, "pw@example.test", "PW Co")
    client(env).call("POST", "/api/pilot/auth/forgot", {"email": "pw@example.test"})
    raw = _sink_token("pw@example.test", mail.PILOT_RESET)
    status, body = client(env).call("POST", "/api/pilot/auth/reset", {"token": raw, "password": "short"})
    assert status == 400 and body == {"error": "weak_password"}
    status, body = client(env).call("POST", "/api/pilot/auth/reset", {"token": "not-a-token", "password": PW})
    assert status == 400 and body == {"error": "invalid_token"}


def test_token_fragment_is_removed_from_browser_history():
    for name in ("contractor-account.js", "login.html"):
        src = (REPO / name).read_text(encoding="utf-8")
        assert "history.replaceState" in src
        assert "takeFragmentToken" in src
