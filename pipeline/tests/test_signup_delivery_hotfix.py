"""Hotfix: contractor signup email delivery is visible, actionable and end-to-end.

Uses production's pilot harness (real HTTP handler, pilot platform database).
"""
from __future__ import annotations

import json
import sqlite3
import ssl

import pytest

from pipeline.api import client_address
from pipeline.auth import mail, throttle
from pipeline.auth import service as auth
from pipeline.auth.seed import ensure_default_organization, seed_auth
from pipeline.tests.test_contractor_pilot import (  # noqa: F401  (pytest fixtures)
    PW, _sink_token, client, db, env, login, signup,
)

OWNER = "owner.contractor@example.test"


def _audit(env, email):
    c = db(env)
    rows = [json.loads(r["details_json"] or "{}") for r in c.execute(
        "SELECT details_json FROM security_audit_log WHERE event_type='email_verification_requested' "
        "ORDER BY id")]
    c.close()
    return [r for r in rows if r.get("email") == email]


def _live_signup_tokens(env, email):
    c = db(env)
    n = c.execute("SELECT COUNT(*) FROM auth_tokens WHERE purpose='pilot_signup' AND email=? "
                  "AND consumed_at IS NULL AND revoked_at IS NULL", (email,)).fetchone()[0]
    c.close()
    return n


def _signup_request(env, email):
    return client(env).call("POST", "/api/pilot/auth/signup",
                            {"name": "Owner Test", "business_name": "Owner Test Plumbing", "email": email,
                             "language": "en", "visit_id": "v-hotfix"})


# ---- end-to-end journey ----------------------------------------------------------------

def test_registration_email_verification_login_and_portal_access(env, capsys):
    c, me = signup(env, OWNER, "Owner Test Plumbing")       # signup -> email -> link -> password
    assert me["kind"] == "contractor" if "kind" in me else True
    out = capsys.readouterr().out
    assert "MAIL_SENT kind=pilot_signup_verification" in out
    assert OWNER not in out and "#t=" not in out             # no address or token in logs
    status, _ = c.call("GET", "/api/pilot/me")
    assert status == 200
    status, _ = c.call("POST", "/api/pilot/auth/logout", {})
    assert status == 200
    c2, me2 = login(env, OWNER)                               # sign in again
    status, created = c2.call("POST", "/api/pilot/contractor/requests",
                              {"title": "Hotfix check", "reference": "HF-1",
                               "lines": [{"raw_description": "3/4in ball valve", "quantity": 4, "uom": "ea"}]})
    assert status == 201, created                             # contractor portal works
    status, items = c2.call("GET", "/api/pilot/contractor/requests")
    assert status == 200 and any(r["request_id"] == created["request_id"] for r in items["items"])
    assert client(env).call("GET", "/contractor-home.html")[0] == 200


# ---- the failure that was invisible -------------------------------------------------------

def test_postmark_rejection_is_logged_with_error_code_and_audited(env, monkeypatch, capsys):
    body = json.dumps({"ErrorCode": 412, "Message": f"Recipient {OWNER} is outside the approved domain"}).encode()
    transport = lambda url, data, headers: (422, body)  # noqa: E731
    monkeypatch.setattr(mail, "get_mailer", lambda: mail.PostmarkMailer("test-token", transport=transport))
    status, resp = _signup_request(env, OWNER)
    assert status == 202 and resp == {"status": "check_email"}   # public answer unchanged
    out = capsys.readouterr().out
    assert "MAIL_DELIVERY_FAILED kind=pilot_signup_verification" in out
    assert "ErrorCode 412" in out and "hint=account pending approval" in out
    assert OWNER not in out and "test-token" not in out and "outside the approved domain" not in out
    rec = _audit(env, OWNER)[-1]
    assert rec["result"] == "mail_unavailable"
    assert rec["mail_error"] == "postmark send rejected (HTTP 422, ErrorCode 412)"
    assert _live_signup_tokens(env, OWNER) == 0                   # the unsent link was revoked


@pytest.mark.parametrize("code,hint", [(406, "recipient inactive"), (400, "sender signature not found"),
                                       (10, "API token")])
def test_common_postmark_errors_get_operator_hints(code, hint, capsys):
    m = mail.PostmarkMailer("t", transport=lambda u, d, h: (422, json.dumps({"ErrorCode": code}).encode()))
    with pytest.raises(mail.MailSendError) as err:
        m.send(mail.Message(to="a@example.test", subject="s", text="t", kind="k"))
    mail.log_outcome("k", err.value)
    assert hint in capsys.readouterr().out


def test_unreachable_provider_is_logged(env, monkeypatch, capsys):
    def boom(url, data, headers):
        raise OSError("network down")
    monkeypatch.setattr(mail, "get_mailer", lambda: mail.PostmarkMailer("t", transport=boom))
    assert _signup_request(env, OWNER)[0] == 202
    out = capsys.readouterr().out
    assert "MAIL_DELIVERY_FAILED" in out and "postmark request failed: OSError" in out


# ---- already registered: notice instead of silence ------------------------------------

def test_existing_account_gets_a_no_token_notice(env):
    signup(env, OWNER, "Owner Test Plumbing")
    mail.sink().clear()
    status, resp = _signup_request(env, OWNER)
    assert status == 202 and resp == {"status": "check_email"}
    notices = mail.sink().outbox(to=OWNER, kind=mail.PILOT_ALREADY_REGISTERED)
    assert len(notices) == 1 and notices[0].link is None
    assert "contractor-account.html?mode=signin" in notices[0].text
    assert "#t=" not in notices[0].text and "token" not in notices[0].text.lower()
    assert mail.sink().outbox(to=OWNER, kind=mail.PILOT_SIGNUP) == []
    assert _audit(env, OWNER)[-1]["result"] == "already_registered_notice_sent"


def test_silent_email_throttle_is_logged(env, capsys):
    for _ in range(throttle.PILOT_LIMITS["signup_email"]):
        _signup_request(env, OWNER)
    capsys.readouterr()
    status, resp = _signup_request(env, OWNER)
    assert status == 202 and resp == {"status": "check_email"}
    out = capsys.readouterr().out
    assert "SIGNUP_THROTTLED scope=email" in out and OWNER not in out


# ---- existing access unaffected --------------------------------------------------------------

def test_supplier_inbox_and_employee_admin_access_unaffected(env):
    inbox, _ = login(env, "inbox-a@example.test")              # pilot supplier account
    assert inbox.call("GET", "/api/pilot/supplier/requests")[0] == 200
    c = sqlite3.connect(env["billing_db"])
    c.row_factory = sqlite3.Row
    seed_auth(c)
    org = ensure_default_organization(c)
    auth.create_user(c, organization_id=org, email="hotfix-admin@example.test", password="Admin-Pass-123",
                     role_names=["admin"], must_change_password=False)
    c.commit()
    c.close()
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", env["port"], timeout=20)
    conn.request("POST", "/api/auth/login", json.dumps({"email": "hotfix-admin@example.test",
                                                        "password": "Admin-Pass-123"}),
                 {"Content-Type": "application/json"})
    res = conn.getresponse()
    res.read()
    session = next(v.split(";", 1)[0] for h, v in res.getheaders()
                   if h.lower() == "set-cookie" and v.startswith("corridoriq_session="))
    conn.close()
    assert res.status == 200
    staff = client(env)
    staff.staff_cookie = session
    assert staff.call("GET", "/api/admin/users")[0] == 200


# ---- transport and client address -------------------------------------------------------------

def test_postmark_transport_verifies_tls(monkeypatch):
    seen = {}

    class _Resp:
        status = 200
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["ctx"] = context
        return _Resp()

    monkeypatch.setattr(mail.urllib.request, "urlopen", fake_urlopen)
    mail._default_transport(mail.POSTMARK_API_URL, b"{}", {})
    assert seen["ctx"].verify_mode == ssl.CERT_REQUIRED and seen["ctx"].check_hostname


class _H(dict):
    def get(self, key, default=None):
        return next((v for k, v in self.items() if k.lower() == key.lower()), default)


@pytest.mark.parametrize("peer,headers,source,expected", [
    ("100.64.0.7", {"X-Real-IP": "1.1.1.1"}, "railway", "1.1.1.1"),
    ("100.64.0.7", {"X-Real-IP": "1.1.1.1"}, None, "100.64.0.7"),
    ("8.8.8.8", {"X-Real-IP": "9.9.9.9"}, "railway", "8.8.8.8"),
    ("100.64.0.7", {"X-Real-IP": "10.0.0.5"}, "railway", "100.64.0.7"),
    ("100.64.0.7", {"X-Forwarded-For": "8.8.8.8"}, "railway", "100.64.0.7"),
    ("100.64.0.7", {"CF-Connecting-IP": "8.8.8.8"}, "railway", "100.64.0.7"),
])
def test_railway_client_address(peer, headers, source, expected):
    assert client_address.resolve_client_ip(peer, _H(headers), secret="", hosted=False, source=source) == expected


def test_shared_hop_no_longer_caps_platform_signups():
    t = throttle.Throttle({"signup_ip": 10})
    for _ in range(10):
        assert not t.hit("signup_ip", "100.64.0.7")
    assert not t.hit("signup_ip", "100.64.0.7")          # 11th contractor not refused
    for _ in range(9):
        t.hit("signup_ip", "8.8.8.8")
    assert t.hit("signup_ip", "8.8.8.8") is False and t.hit("signup_ip", "8.8.8.8") is True
