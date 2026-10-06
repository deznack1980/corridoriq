"""Phase 2 identity hardening: tokens, mail sink, invitation, reset, entitlement."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.api import server as server_mod
from pipeline.auth import mail, tokens, throttle
from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.billing import contractor_accounts as ca
from pipeline.config.settings import SCHEMA_PATH
from pipeline.pilot import accounts, materials
from pipeline.pilot.platform import ENV_ROOT, Platform, auth_schema_sql
from pipeline.tests.test_billing import _login, _req
from pipeline.tests.test_contractor_pilot import (
    PW, _billing, _contractor_billing, _request, _sink_token, _use_test_billing,
    client, db, login, new_request, send, signup,
)

REPO = Path(__file__).resolve().parents[2]


def _now():
    return datetime.now(timezone.utc)


def _token_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(auth_schema_sql())
    return conn


def test_token_hash_is_persisted_and_raw_is_not():
    conn = _token_db()
    raw, expires = tokens.issue(conn, purpose=tokens.PILOT_SIGNUP, ttl=tokens.SIGNUP_TTL,
                                email="a@example.test")
    conn.commit()
    row = conn.execute("SELECT * FROM auth_tokens").fetchone()
    assert row["token_hash"] == tokens.hash_token(raw)
    blob = json.dumps(dict(row))
    assert raw not in blob
    assert datetime.fromisoformat(expires) > _now()


@pytest.mark.parametrize("raw,purpose,email", [
    ("not-a-token!", tokens.PILOT_SIGNUP, "a@example.test"),
    ("", tokens.PILOT_SIGNUP, "a@example.test"),
    ("x" * 200, tokens.PILOT_SIGNUP, "a@example.test"),
])
def test_malformed_tokens_are_rejected(raw, purpose, email):
    conn = _token_db()
    tokens.issue(conn, purpose=purpose, ttl=tokens.SIGNUP_TTL, email=email)
    conn.commit()
    assert tokens.inspect(conn, raw, purpose=purpose) is None
    with pytest.raises(tokens.TokenError):
        tokens.consume(conn, raw, purpose=purpose, email=email)


def test_wrong_purpose_expired_consumed_and_subject_are_rejected():
    conn = _token_db()
    raw, _ = tokens.issue(conn, purpose=tokens.PILOT_SIGNUP, ttl=tokens.SIGNUP_TTL,
                          email="a@example.test")
    conn.commit()
    assert tokens.inspect(conn, raw, purpose=tokens.PILOT_RESET) is None
    with pytest.raises(tokens.TokenError):
        tokens.consume(conn, raw, purpose=tokens.PILOT_RESET, email="a@example.test")
    with pytest.raises(tokens.TokenError):
        tokens.consume(conn, raw, purpose=tokens.PILOT_SIGNUP, email="other@example.test")
    conn.execute("UPDATE auth_tokens SET expires_at=?", ("2000-01-01T00:00:00+00:00",))
    conn.commit()
    assert tokens.inspect(conn, raw, purpose=tokens.PILOT_SIGNUP) is None
    raw2, _ = tokens.issue(conn, purpose=tokens.PILOT_SIGNUP, ttl=tokens.SIGNUP_TTL,
                           email="b@example.test")
    conn.commit()
    with tokens.transaction(conn):
        tokens.consume(conn, raw2, purpose=tokens.PILOT_SIGNUP, email="b@example.test")
    with pytest.raises(tokens.TokenError):
        tokens.consume(conn, raw2, purpose=tokens.PILOT_SIGNUP, email="b@example.test")


def test_inspect_does_not_consume():
    conn = _token_db()
    raw, _ = tokens.issue(conn, purpose=tokens.PILOT_VERIFY, ttl=tokens.VERIFY_TTL,
                          user_id=None, email="v@example.test")
    conn.commit()
    assert tokens.inspect(conn, raw, purpose=tokens.PILOT_VERIFY) is not None
    assert tokens.inspect(conn, raw, purpose=tokens.PILOT_VERIFY)["consumed_at"] is None
    row = conn.execute("SELECT consumed_at FROM auth_tokens").fetchone()
    assert row["consumed_at"] is None


def test_replacement_revokes_outstanding_token():
    conn = _token_db()
    first, _ = tokens.issue(conn, purpose=tokens.PILOT_SIGNUP, ttl=tokens.SIGNUP_TTL,
                            email="r@example.test")
    second, _ = tokens.issue(conn, purpose=tokens.PILOT_SIGNUP, ttl=tokens.SIGNUP_TTL,
                             email="r@example.test")
    conn.commit()
    assert tokens.inspect(conn, first, purpose=tokens.PILOT_SIGNUP) is None
    assert tokens.inspect(conn, second, purpose=tokens.PILOT_SIGNUP) is not None


def test_failed_mutation_rolls_back_token_consumption():
    conn = _token_db()
    raw, _ = tokens.issue(conn, purpose=tokens.PILOT_SIGNUP, ttl=tokens.SIGNUP_TTL,
                          email="tx@example.test")
    conn.commit()
    try:
        with tokens.transaction(conn):
            tokens.consume(conn, raw, purpose=tokens.PILOT_SIGNUP, email="tx@example.test")
            raise RuntimeError("account mutation failed")
    except RuntimeError:
        pass
    row = conn.execute("SELECT consumed_at FROM auth_tokens").fetchone()
    assert row["consumed_at"] is None
    with tokens.transaction(conn):
        tokens.consume(conn, raw, purpose=tokens.PILOT_SIGNUP, email="tx@example.test")
    assert conn.execute("SELECT consumed_at FROM auth_tokens").fetchone()["consumed_at"]


def test_successful_consume_happens_exactly_once():
    conn = _token_db()
    raw, _ = tokens.issue(conn, purpose=tokens.PORTAL_RESET, ttl=tokens.RESET_TTL,
                          email="once@example.test")
    conn.commit()
    with tokens.transaction(conn):
        tokens.consume(conn, raw, purpose=tokens.PORTAL_RESET, email="once@example.test")
    with pytest.raises(tokens.TokenError):
        with tokens.transaction(conn):
            tokens.consume(conn, raw, purpose=tokens.PORTAL_RESET, email="once@example.test")


def test_password_marker_mismatch_is_rejected():
    conn = _token_db()
    marker = tokens.password_marker("scrypt$old")
    raw, _ = tokens.issue(conn, purpose=tokens.PORTAL_RESET, ttl=tokens.RESET_TTL,
                          email="m@example.test", password_marker=marker)
    conn.commit()
    with pytest.raises(tokens.TokenError):
        tokens.consume(conn, raw, purpose=tokens.PORTAL_RESET, email="m@example.test",
                       password_marker=tokens.password_marker("scrypt$new"))


def test_mail_provider_fails_closed_and_sink_is_in_process_only():
    mail.sink().clear()
    msg = mail.pilot_signup_verification("a@example.test", "http://127.0.0.1/x#t=abc",
                                         "2099-01-01T00:00:00+00:00")
    mail.send(msg)
    assert mail.sink().outbox(to="a@example.test")
    assert mail.provider_name() == mail.SINK
    monkey = pytest.MonkeyPatch()
    monkey.setenv(mail.ENV_PROVIDER, "postmark")
    try:
        with pytest.raises(mail.MailNotConfigured):
            mail.get_mailer()
    finally:
        monkey.undo()
    src = (REPO / "pipeline/pilot/api.py").read_text(encoding="utf-8")
    src += (REPO / "pipeline/api/server.py").read_text(encoding="utf-8")
    assert "outbox" not in src
    for path in ("/api/mail/outbox", "/api/dev/outbox", "/api/pilot/mail/outbox"):
        assert path not in src


def test_invitation_expiry_and_wrong_org_are_rejected():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(c := conn)
    house = c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    other = c.execute(
        "INSERT INTO organizations (name, slug, is_active, account_type, created_at, updated_at) "
        "VALUES ('Other','other-co',1,'contractor',?,?)", (now, now)).lastrowid
    op = auth.create_user(c, organization_id=house, email="op@example.test",
                          password="Passw0rd!x", role_names=["admin"], must_change_password=False)
    operator = auth.build_user_context(c, c.execute("SELECT * FROM users WHERE id=?", (op,)).fetchone())
    res = ca.create_contractor_account(c, operator, {
        "company_name": "Invite Expire", "contact_name": "Owner Person",
        "email": "exp@example.test", "business_zip": "85004",
    })
    raw = mail.sink().outbox(to="exp@example.test", kind=mail.PORTAL_INVITE)[-1].link.split("#t=", 1)[1]
    c.execute("UPDATE auth_tokens SET context_json=? WHERE purpose=?",
              (json.dumps({"organization_id": other}), tokens.PORTAL_INVITE))
    c.commit()
    with pytest.raises(auth.AuthError):
        ca.accept_owner_invitation(c, raw, "Owner-Chosen-Pass-123")
    assert tokens.inspect(c, raw, purpose=tokens.PORTAL_INVITE) is not None
    c.execute("UPDATE auth_tokens SET expires_at='2000-01-01T00:00:00+00:00'")
    c.commit()
    with pytest.raises(auth.AuthError):
        ca.accept_owner_invitation(c, raw, "Owner-Chosen-Pass-123")
    assert res["invitation_sent"] is True


def test_pilot_auth_token_indexes_are_copied():
    sql = auth_schema_sql()
    assert "idx_auth_tokens_user" in sql and "idx_auth_tokens_email" in sql


def test_apply_email_change_clears_verification_and_revokes_tokens():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(conn)
    org = conn.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    uid = auth.create_user(conn, organization_id=org, email="old@example.test",
                           password=PW, role_names=["admin"], must_change_password=False)
    auth.mark_email_verified(conn, uid, "old@example.test")
    raw, _ = tokens.issue(conn, purpose=tokens.PORTAL_RESET, ttl=tokens.RESET_TTL,
                          user_id=uid, email="old@example.test")
    conn.commit()
    auth.apply_email_change(conn, uid, "New@Example.TEST")
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    assert row["normalized_email"] == "new@example.test"
    assert not auth.email_is_verified(row)
    assert tokens.inspect(conn, raw, purpose=tokens.PORTAL_RESET) is None


# ---- HTTP flows reuse the contractor-pilot env fixture ----------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "pilot"
    monkeypatch.setenv(ENV_ROOT, str(root))
    billing_db = tmp_path / "billing.db"
    init = sqlite3.connect(billing_db)
    init.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
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
    accounts.create_supplier(platform, "supplier-b", "Synthetic Supply B")
    code_a = accounts.create_referral_code(conn, platform, "supplier-a", "supa", "counter")
    code_b = accounts.create_referral_code(conn, platform, "supplier-b", "supb")
    accounts.add_supplier_user(conn, platform, "supplier-a", "inbox-a@example.test", PW, "Inbox A")
    accounts.add_supplier_user(conn, platform, "supplier-b", "inbox-b@example.test", PW, "Inbox B")
    conn.close()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"root": root, "platform": platform, "port": srv.server_address[1], "code_a": code_a,
           "code_b": code_b, "billing_db": billing_db}
    srv.shutdown()
    srv.server_close()


def test_signup_is_generic_and_creates_no_session_until_complete(env):
    from pipeline.auth import mail
    c = client(env)
    status, resp = c.call("POST", "/api/pilot/auth/signup", {
        "name": "Pat", "business_name": "Pat Pipe", "email": "pat@example.test",
    })
    assert status == 202 and resp == {"status": "check_email"}
    assert c.cookie is None
    assert c.call("GET", "/api/pilot/me")[0] == 401
    assert c.call("GET", "/api/pilot/auth/signup")[0] in (401, 404)
    raw = _sink_token("pat@example.test", mail.PILOT_SIGNUP)
    inspect = c.call("POST", "/api/pilot/auth/inspect", {"token": raw, "purpose": tokens.PILOT_SIGNUP})
    assert inspect == (200, {"valid": True, "purpose": tokens.PILOT_SIGNUP})
    assert c.call("POST", "/api/pilot/auth/inspect", {"token": raw, "purpose": tokens.PILOT_SIGNUP})[1]["valid"]
    status, me = c.call("POST", "/api/pilot/auth/complete-signup", {"token": raw, "password": PW})
    assert status == 201
    assert me["contractor"]["email_verification"] == "verified"
    row = db(env).execute("SELECT * FROM users WHERE normalized_email='pat@example.test'").fetchone()
    assert row["email_verified_address"] == "pat@example.test"
    assert tokens.hash_token(raw) == db(env).execute(
        "SELECT token_hash FROM auth_tokens WHERE purpose=?", (tokens.PILOT_SIGNUP,)).fetchone()["token_hash"]
    blob = json.dumps(me)
    assert raw not in blob


def test_legacy_accounts_stay_unverified_until_they_complete(env):
    from pipeline.auth import mail
    c, me = signup(env, "legacy@example.test", "Legacy Co")
    conn = db(env)
    conn.execute("UPDATE users SET email_verified_at=NULL, email_verified_address=NULL "
                 "WHERE normalized_email='legacy@example.test'")
    conn.commit()
    conn.close()
    me = c.call("GET", "/api/pilot/me")[1]
    assert me["contractor"]["email_verification"] == "pending"
    assert c.call("POST", "/api/pilot/auth/request-verification", {}) == (202, {"status": "check_email"})
    raw = _sink_token("legacy@example.test", mail.PILOT_VERIFY)
    assert c.call("GET", "/api/pilot/auth/verify-email")[0] in (401, 404, 405)
    assert c.call("POST", "/api/pilot/auth/verify-email", {"token": raw})[0] == 200
    me = c.call("GET", "/api/pilot/me")[1]
    assert me["contractor"]["email_verification"] == "verified"


def test_pilot_password_reset_revokes_sessions_and_does_not_auto_login(env):
    from pipeline.auth import mail
    c, _ = signup(env, "reset@example.test", "Reset Co")
    old_cookie = c.cookie
    assert client(env).call("POST", "/api/pilot/auth/forgot", {"email": "nobody@example.test"}) == (
        202, {"status": "check_email"})
    assert client(env).call("POST", "/api/pilot/auth/forgot", {"email": "reset@example.test"}) == (
        202, {"status": "check_email"})
    raw = _sink_token("reset@example.test", mail.PILOT_RESET)
    other = client(env)
    status, body = other.call("POST", "/api/pilot/auth/reset", {"token": raw, "password": "Brand-New-Pass-123"})
    assert status == 200 and body == {"ok": True}
    assert other.cookie is None
    c2 = client(env)
    c2.cookie = old_cookie
    assert c2.call("GET", "/api/pilot/me")[0] == 401
    assert client(env).call("POST", "/api/pilot/auth/login",
                            {"email": "reset@example.test", "password": PW})[0] == 401
    status, me = client(env).call("POST", "/api/pilot/auth/login",
                                 {"email": "reset@example.test", "password": "Brand-New-Pass-123"})
    assert status == 200 and me["user"]["email"] == "reset@example.test"


def test_rate_limit_uses_phase1a_ip_and_normalizes_email(env, monkeypatch):
    secret = "phase2-proxy-secret"
    monkeypatch.setenv("CORRIDORIQ_TRUSTED_PROXY_SECRET", secret)
    throttle.pilot.clear()
    c = client(env)
    trusted = {"CF-Connecting-IP": "203.0.113.40", "X-CorridorIQ-Proxy-Secret": secret}
    for i in range(5):
        status, _ = c.call("POST", "/api/pilot/auth/signup", {
            "name": "N", "business_name": "Biz", "email": "Limit@Example.TEST",
        }, headers=trusted)
        assert status == 202
    status, body = c.call("POST", "/api/pilot/auth/signup", {
        "name": "N", "business_name": "Biz", "email": "limit@example.test",
    }, headers=trusted)
    assert status == 202 and body == {"status": "check_email"}
    forged = {"X-Forwarded-For": "198.51.100.9", "CF-Connecting-IP": "203.0.113.41"}
    for _ in range(10):
        c.call("POST", "/api/pilot/auth/signup", {"email": "xff@example.test"}, headers=forged)
    assert c.call("POST", "/api/pilot/auth/signup", {"email": "xff2@example.test"},
                  headers=forged)[0] == 429


def test_unverified_or_mismatched_or_non_owner_entitlement_is_standard(env, monkeypatch):
    _use_test_billing(monkeypatch)
    cases = [
        ("unv@example.test", dict(verified=False), "Unverified Main"),
        ("mis@example.test", dict(verified=True), "Mismatch Co"),
        ("mem@example.test", dict(owner=False), "Member Co"),
        ("sup@example.test", dict(account_type="supplier"), "Supplier Org"),
    ]
    for email, kwargs, business in cases:
        _contractor_billing(env, email, **kwargs)
        c, me = signup(env, email, business, code=env["code_a"])
        if email.startswith("mis"):
            conn = _billing(env)
            conn.execute("UPDATE users SET email_verified_address='other@example.test' "
                         "WHERE normalized_email=?", (email,))
            conn.commit()
            conn.close()
        assert _request(c)[1]["priority"] == "standard", email


def test_inactive_user_or_org_and_duplicate_identities_are_standard(env, monkeypatch):
    _use_test_billing(monkeypatch)
    email = "dup-main@example.test"
    _contractor_billing(env, email)
    c, _ = signup(env, email, "Dup Main", code=env["code_a"])
    original = materials._open_billing_lookup

    def fake_lookup():
        conn = original()
        real = conn.execute

        def execute(sql, params=()):
            if isinstance(sql, str) and "contractor_owner" in sql and "normalized_email" in sql:
                class Rows:
                    def fetchall(self):
                        return [{"organization_id": 1}, {"organization_id": 2}]
                return Rows()
            return real(sql, params)

        conn.execute = execute
        return conn

    monkeypatch.setattr(materials, "_open_billing_lookup", fake_lookup)
    assert _request(c)[1]["priority"] == "standard"
    email2 = "inactive@example.test"
    org = _contractor_billing(env, email2)
    conn = _billing(env)
    conn.execute("UPDATE users SET is_active=0 WHERE organization_id=?", (org,))
    conn.commit()
    conn.close()
    c2, _ = signup(env, email2, "Inactive User", code=env["code_a"])
    assert _request(c2)[1]["priority"] == "standard"
    email3 = "dead-org@example.test"
    org3 = _contractor_billing(env, email3)
    conn = _billing(env)
    conn.execute("UPDATE organizations SET is_active=0 WHERE id=?", (org3,))
    conn.commit()
    conn.close()
    c3, _ = signup(env, email3, "Dead Org", code=env["code_a"])
    assert _request(c3)[1]["priority"] == "standard"


def test_billing_error_and_wrong_mode_are_standard(env, monkeypatch):
    _use_test_billing(monkeypatch)
    email = "bill-err@example.test"
    _contractor_billing(env, email)
    monkeypatch.setattr("pipeline.billing.entitlements.request_priority",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("billing down")))
    c, _ = signup(env, email, "Billing Err", code=env["code_a"])
    assert _request(c, priority="priority")[1]["priority"] == "standard"


def test_send_time_upgrades_and_historical_priority_survives_cancel(env, monkeypatch):
    _use_test_billing(monkeypatch)
    email = "send-time@example.test"
    org = _contractor_billing(env, email, with_account=False)
    c, me = signup(env, email, "Send Time Co", code=env["code_a"])
    draft = new_request(c, "Later paid")
    assert draft["priority"] == "standard"
    conn = _billing(env)
    now = "2026-10-05T12:00:00+00:00"
    conn.execute(
        """
        INSERT INTO billing_accounts (
            organization_id, stripe_customer_id, stripe_subscription_id, stripe_price_id,
            subscription_status, billing_state, livemode, current_period_end, created_at, updated_at
        ) VALUES (?, 'cus_st', 'sub_st', ?, 'active', 'active', 0, '2099-01-01T00:00:00+00:00', ?, ?)
        """,
        (org, __import__("pipeline.tests.test_billing_contractor_pro", fromlist=["PRICE_C"]).PRICE_C, now, now),
    )
    conn.commit()
    conn.close()
    edited = c.call("PATCH", f"/api/pilot/contractor/requests/{draft['request_id']}",
                    {"title": "Later paid", "lines": [{"raw_description": "pipe", "quantity": 1}]})[1]
    assert edited["priority"] == "priority"
    status, sent = send(c, draft, me["connections"][0]["connection_id"])
    assert status == 200 and sent["priority"] == "priority"
    conn = _billing(env)
    conn.execute("UPDATE billing_accounts SET billing_state='canceled', subscription_status='canceled' "
                 "WHERE organization_id=?", (org,))
    conn.commit()
    conn.close()
    mine = c.call("GET", f"/api/pilot/contractor/requests/{draft['request_id']}")[1]
    assert mine["priority"] == "priority"
    supplier, _ = login(env, "inbox-a@example.test")
    inbox = supplier.call("GET", "/api/pilot/supplier/requests")[1]["items"]
    assert inbox[0]["priority"] == "priority"


def test_cross_tenant_token_and_forged_client_fields(env, monkeypatch):
    from pipeline.auth import mail
    _use_test_billing(monkeypatch)
    a, _ = signup(env, "aa@example.test", "AA Co")
    b, _ = signup(env, "bb@example.test", "BB Co")
    conn = db(env)
    conn.execute("UPDATE users SET email_verified_at=NULL, email_verified_address=NULL "
                 "WHERE normalized_email='aa@example.test'")
    conn.commit()
    conn.close()
    assert a.call("POST", "/api/pilot/auth/request-verification", {})[0] == 202
    raw = _sink_token("aa@example.test", mail.PILOT_VERIFY)
    assert b.call("POST", "/api/pilot/auth/verify-email", {"token": raw})[0] == 400
    _contractor_billing(env, "aa@example.test")
    status, req = a.call("POST", "/api/pilot/contractor/requests", {
        "title": "Job", "lines": [{"raw_description": "pipe", "quantity": 1}],
        "priority": "priority", "organization_id": 1, "plan": "contractor_pro",
        "entitlement": "contractor_pro", "account_type": "contractor", "role": "contractor_owner",
    })
    assert status == 201 and req["priority"] == "standard"


def test_no_public_mail_outbox_route(env):
    for path in ("/api/mail/outbox", "/api/dev/outbox", "/api/pilot/mail/outbox", "/api/auth/outbox"):
        assert client(env).call("GET", path)[0] in (401, 404)


def test_priority_copy_does_not_promise_supplier_performance():
    text = (REPO / "for-contractors.html").read_text(encoding="utf-8")
    text += (REPO / "billing.js").read_text(encoding="utf-8")
    assert "listed first and marked Priority in your supplier's CorridorIQ inbox" in text
    lowered = text.lower()
    for banned in ("guaranteed faster", "we guarantee", "guaranteed quote", "supplier sla"):
        assert banned not in lowered


def _portal_site(tmp_path, monkeypatch):
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


def test_portal_password_reset_and_invitation_acceptance(tmp_path, monkeypatch):
    srv, factory = _portal_site(tmp_path, monkeypatch)
    try:
        port = srv.server_address[1]
        monkeypatch.setenv("CORRIDORIQ_BILLING_ENABLED", "1")
        admin = _login(port, "reset-main@example.test")
        s, _, raw = _req(port, "POST", "/api/auth/forgot", {"email": "reset-main@example.test"}, J)
        assert s == 202 and json.loads(raw) == {"status": "check_email"}
        token = mail.sink().outbox(to="reset-main@example.test", kind=mail.PORTAL_RESET)[-1].link.split("#t=", 1)[1]
        s, _, body = _req(port, "POST", "/api/auth/reset", {"token": token, "password": "Main-Reset-Pass-123"}, J)
        assert s == 200 and json.loads(body) == {"ok": True}
        assert _req(port, "GET", "/api/auth/me", None, {"Cookie": admin})[0] == 401
        s, headers, _ = _req(port, "POST", "/api/auth/login",
                             {"email": "reset-main@example.test", "password": "Main-Reset-Pass-123"}, J)
        assert s == 200
        cookie = headers["set-cookie"].split(";")[0]
        assert _req(port, "GET", "/api/auth/me", None, {"Cookie": cookie})[0] == 200

        conn = factory()
        operator = auth.build_user_context(conn, conn.execute(
            "SELECT * FROM users WHERE normalized_email='reset-main@example.test'").fetchone())
        res = ca.create_contractor_account(conn, operator, {
            "company_name": "Invite Co", "contact_name": "Owner Person",
            "email": "invitee@example.test", "business_zip": "85004",
        })
        conn.close()
        assert "temporary_password" not in res and res["invitation_sent"] is True
        invite = mail.sink().outbox(to="invitee@example.test", kind=mail.PORTAL_INVITE)[-1].link.split("#t=", 1)[1]
        s, _, body = _req(port, "POST", "/api/auth/accept-invite",
                          {"token": invite, "password": "Owner-Chosen-Pass-123"}, J)
        assert s == 200 and json.loads(body) == {"ok": True}
        s, _, body = _req(port, "POST", "/api/auth/accept-invite",
                          {"token": invite, "password": "Owner-Chosen-Pass-123"}, J)
        assert s == 400
        s, headers, _ = _req(port, "POST", "/api/auth/login",
                             {"email": "invitee@example.test", "password": "Owner-Chosen-Pass-123"}, J)
        assert s == 200
        cookie = headers["set-cookie"].split(";")[0]
        s, _, raw = _req(port, "GET", "/api/auth/me", None, {"Cookie": cookie})
        payload = json.loads(raw)
        assert s == 200 and payload["user"]["email_verified"] is True
        assert payload["user"]["must_change_password"] is False
    finally:
        srv.shutdown()
        srv.server_close()


J = {"Content-Type": "application/json"}
