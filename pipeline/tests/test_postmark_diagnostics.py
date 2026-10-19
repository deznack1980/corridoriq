"""Diagnostic logs for contractor signup mail. Production responses stay unchanged.

No network and no real Postmark token. The public signup answer remains
check_email, an existing account still sends nothing, and rate limits are the
production limits.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from pipeline.auth import mail, throttle
from pipeline.tests.test_contractor_pilot import (  # noqa: F401
    PW, client, db, env, signup,
)

OWNER = "owner.contractor@example.test"
SECRET = "server-token-value"


def _fp(token: str = SECRET) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def _signup(env, email):
    return client(env).call(
        "POST", "/api/pilot/auth/signup",
        {"name": "Owner Test", "business_name": "Owner Test Plumbing", "email": email,
         "language": "en", "visit_id": "v-diag"},
    )


def _live_tokens(env, email):
    conn = db(env)
    count = conn.execute(
        "SELECT COUNT(*) FROM auth_tokens WHERE purpose='pilot_signup' AND email=? "
        "AND consumed_at IS NULL AND revoked_at IS NULL",
        (email,),
    ).fetchone()[0]
    conn.close()
    return count


def _audit(env, email):
    conn = db(env)
    rows = [json.loads(row["details_json"] or "{}") for row in conn.execute(
        "SELECT details_json FROM security_audit_log WHERE event_type='email_verification_requested' "
        "ORDER BY id")]
    conn.close()
    return [row for row in rows if row.get("email") == email]


def _install(monkeypatch, status, payload=None, exc=None):
    raw = b""
    if payload is not None:
        raw = json.dumps(payload).encode() if isinstance(payload, dict) else str(payload).encode()

    def transport(_url, _data, _headers):
        if exc is not None:
            raise exc
        return status, raw

    monkeypatch.setattr(mail, "get_mailer", lambda: mail.PostmarkMailer(SECRET, transport=transport))


def _assert_clean(out, *extra):
    assert SECRET not in out
    assert "X-Postmark-Server-Token" not in out
    assert "#t=" not in out
    assert "@" not in out
    for item in extra:
        assert item not in out


def test_successful_postmark_response_logs_message_id(env, monkeypatch, capsys):
    _install(monkeypatch, 200, {
        "ErrorCode": 0, "Message": f"delivered to {OWNER}", "MessageID": "mid-123", "To": OWNER,
    })
    email = "new.contractor@example.test"
    status, resp = _signup(env, email)
    assert status == 202 and resp == {"status": "check_email"}
    out = capsys.readouterr().out
    assert "MAIL_SEND_ATTEMPT" in out and "MAIL_SENT" in out
    assert "message_id=mid-123" in out and "http_status=200" in out
    assert "error_code=0" in out and "category=accepted" in out
    assert "stream=outbound" in out and "from_domain=corridoriq.pro" in out
    assert f"token_fp={_fp()}" in out and "request_id=" in out and "ts=" in out
    assert "kind=pilot_signup_verification" in out
    _assert_clean(out, email, "delivered to", OWNER)
    assert _live_tokens(env, email) == 1
    assert _audit(env, email)[-1]["result"] == "sent"


@pytest.mark.parametrize("status_code,payload,category,code", [
    (401, {"ErrorCode": 10, "Message": "Invalid token secret"}, "bad_token", 10),
    (422, {"ErrorCode": 400, "Message": "Sender signature not found"}, "sender_not_found", 400),
    (422, {"ErrorCode": 401, "Message": "Sender signature not confirmed"}, "sender_unconfirmed", 401),
    (422, {"ErrorCode": 422, "Message": "The stream does not exist"}, "invalid_stream", 422),
    (400, {"Message": "bad request body"}, "http_400", None),
    (401, {"Message": "unauthorized body"}, "http_401", None),
    (422, {"Message": "unprocessable body"}, "http_422", None),
])
def test_postmark_rejections_are_logged_and_revoke_the_token(
        env, monkeypatch, capsys, status_code, payload, category, code):
    _install(monkeypatch, status_code, payload)
    email = f"{category}@example.test"
    status, resp = _signup(env, email)
    assert status == 202 and resp == {"status": "check_email"}
    out = capsys.readouterr().out
    assert "MAIL_SEND_ATTEMPT" in out and "MAIL_DELIVERY_FAILED" in out
    assert f"http_status={status_code}" in out and f"category={category}" in out
    assert "stream=outbound" in out and f"token_fp={_fp()}" in out
    if code is None:
        assert "error_code=-" in out
    else:
        assert f"error_code={code}" in out
    assert f"postmark send rejected (HTTP {status_code})" in out
    _assert_clean(out, email, payload.get("Message", ""))
    assert _live_tokens(env, email) == 0
    assert _audit(env, email)[-1]["result"] == "mail_unavailable"


def test_postmark_timeout_logs_timeout_and_revokes_the_token(env, monkeypatch, capsys):
    _install(monkeypatch, 0, exc=TimeoutError("timed out contacting api.postmarkapp.com"))
    email = "timeout@example.test"
    assert _signup(env, email) == (202, {"status": "check_email"})
    out = capsys.readouterr().out
    assert "MAIL_DELIVERY_FAILED" in out and "category=timeout" in out
    assert "postmark request failed: TimeoutError" in out
    _assert_clean(out, email, "api.postmarkapp.com")
    assert _live_tokens(env, email) == 0


def test_existing_account_still_sends_nothing(env, capsys):
    signup(env, OWNER, "Owner Test Plumbing")
    capsys.readouterr()
    before = len(mail.sink().outbox(to=OWNER))
    status, resp = _signup(env, OWNER)
    assert status == 202 and resp == {"status": "check_email"}
    assert len(mail.sink().outbox(to=OWNER)) == before
    out = capsys.readouterr().out
    assert "MAIL_SEND_ATTEMPT" not in out
    assert "MAIL_SENT" not in out
    assert "MAIL_DELIVERY_FAILED" not in out
    assert _audit(env, OWNER)[-1]["result"] == "already_registered"
    _assert_clean(out, OWNER)


def test_email_rate_limit_logs_without_sending(env, capsys):
    email = "limited@example.test"
    for _ in range(throttle.PILOT_LIMITS["signup_email"]):
        assert _signup(env, email)[0] == 202
    capsys.readouterr()
    before = len(mail.sink().outbox(to=email, kind=mail.PILOT_SIGNUP))
    status, resp = _signup(env, email)
    assert status == 202 and resp == {"status": "check_email"}
    assert len(mail.sink().outbox(to=email, kind=mail.PILOT_SIGNUP)) == before
    out = capsys.readouterr().out
    assert "MAIL_RATE_LIMITED" in out and "scope=email" in out and "category=rate_limited" in out
    assert "MAIL_SEND_ATTEMPT" not in out
    _assert_clean(out, email)


def test_ip_rate_limit_still_returns_429(env, capsys):
    for n in range(throttle.PILOT_LIMITS["signup_ip"]):
        assert _signup(env, f"ip{n}@example.test")[0] == 202
    capsys.readouterr()
    status, resp = _signup(env, "one-more@example.test")
    assert status == 429
    out = capsys.readouterr().out
    assert "MAIL_RATE_LIMITED" in out and "scope=ip" in out
    assert "MAIL_SEND_ATTEMPT" not in out
    _assert_clean(out, "one-more@example.test")
