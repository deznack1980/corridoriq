"""Postmark transactional-email adapter.

Covers message construction, the From identity, safe failure handling, token
confinement to the request header, provider selection, and that an identity
flow stays fail-closed (generic response, token revoked, no secret leaked)
when Postmark delivery fails. No network and no real token are used.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from pipeline.auth import mail
from pipeline.auth import service as auth
from pipeline.auth import tokens
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import SCHEMA_PATH

FAKE_TOKEN = "pm-not-a-real-server-token"


class _Capture:
    """Fake transport: records the single outbound call, returns 200."""

    def __init__(self, status=200, exc=None):
        self.status = status
        self.exc = exc
        self.calls = []

    def __call__(self, url, data, headers):
        self.calls.append({"url": url, "data": data, "headers": headers})
        if self.exc is not None:
            raise self.exc
        return self.status, b'{"ErrorCode":0,"Message":"OK"}'


def _msg():
    return mail.pilot_password_reset("User@Example.TEST".lower(),
                                     "https://app.corridoriq.pro/contractor-account.html?mode=reset#t=abc123",
                                     "2099-01-01T00:00:00+00:00")


def test_postmark_builds_expected_transactional_message():
    cap = _Capture()
    mailer = mail.PostmarkMailer(FAKE_TOKEN, transport=cap)
    m = _msg()
    mailer.send(m)
    assert len(cap.calls) == 1
    call = cap.calls[0]
    assert call["url"] == mail.POSTMARK_API_URL
    payload = json.loads(call["data"].decode("utf-8"))
    assert payload == {
        "From": "CorridorIQ <noreply@corridoriq.pro>",
        "To": m.to,
        "Subject": m.subject,
        "TextBody": m.text,
        "MessageStream": "outbound",
    }
    assert call["headers"]["X-Postmark-Server-Token"] == FAKE_TOKEN
    assert call["headers"]["Content-Type"] == "application/json"


def test_default_from_identity_and_overrides(monkeypatch):
    cap = _Capture()
    assert mail.PostmarkMailer(FAKE_TOKEN, transport=cap).build_payload(_msg())["From"] == \
        "CorridorIQ <noreply@corridoriq.pro>"
    # explicit override
    p = mail.PostmarkMailer(FAKE_TOKEN, sender="CorridorIQ Ops <ops@corridoriq.pro>",
                            stream="broadcast", transport=cap).build_payload(_msg())
    assert p["From"] == "CorridorIQ Ops <ops@corridoriq.pro>" and p["MessageStream"] == "broadcast"
    # env override
    monkeypatch.setenv(mail.ENV_MAIL_FROM, "CorridorIQ <alerts@corridoriq.pro>")
    monkeypatch.setenv(mail.ENV_POSTMARK_STREAM, "outbound-2")
    p2 = mail.PostmarkMailer(FAKE_TOKEN, transport=cap).build_payload(_msg())
    assert p2["From"] == "CorridorIQ <alerts@corridoriq.pro>" and p2["MessageStream"] == "outbound-2"


def test_recipient_subject_body_passed_through():
    cap = _Capture()
    m = mail.portal_owner_invitation("owner@example.test", "https://app.corridoriq.pro/login.html?mode=accept-invite#t=xyz",
                                     "2099-01-01T00:00:00+00:00", "Acme Plumbing")
    mail.PostmarkMailer(FAKE_TOKEN, transport=cap).send(m)
    payload = json.loads(cap.calls[0]["data"].decode("utf-8"))
    assert payload["To"] == "owner@example.test"
    assert payload["Subject"] == m.subject
    assert payload["TextBody"] == m.text and "Acme Plumbing" in payload["TextBody"]


@pytest.mark.parametrize("cap", [
    _Capture(exc=OSError("connection refused")),
    _Capture(exc=TimeoutError("timed out")),
    _Capture(status=422),
    _Capture(status=500),
])
def test_provider_failure_raises_safely_without_secrets(cap):
    mailer = mail.PostmarkMailer(FAKE_TOKEN, transport=cap)
    with pytest.raises(mail.MailSendError) as ei:
        mailer.send(_msg())
    text = str(ei.value)
    assert FAKE_TOKEN not in text
    assert "abc123" not in text  # the raw reset token in the link
    assert "noreply@corridoriq.pro" not in text


def test_token_never_in_payload_or_repr():
    cap = _Capture()
    mailer = mail.PostmarkMailer(FAKE_TOKEN, transport=cap)
    mailer.send(_msg())
    body = cap.calls[0]["data"].decode("utf-8")
    assert FAKE_TOKEN not in body          # token only in the header
    assert FAKE_TOKEN not in repr(mailer)  # not surfaced via repr


def test_get_mailer_selects_postmark_only_with_token(monkeypatch):
    monkeypatch.setenv("CORRIDORIQ_ENV", "test")
    monkeypatch.setenv(mail.ENV_PROVIDER, "postmark")
    monkeypatch.delenv(mail.ENV_POSTMARK_TOKEN, raising=False)
    with pytest.raises(mail.MailNotConfigured):
        mail.get_mailer()
    monkeypatch.setenv(mail.ENV_POSTMARK_TOKEN, FAKE_TOKEN)
    m = mail.get_mailer()
    assert isinstance(m, mail.PostmarkMailer)


def test_production_sink_still_fails_closed(monkeypatch):
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.delenv(mail.ENV_PROVIDER, raising=False)  # defaults to sink
    with pytest.raises(mail.MailNotConfigured):
        mail.get_mailer()


def test_send_lowercases_recipient_through_adapter(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(mail, "get_mailer", lambda: mail.PostmarkMailer(FAKE_TOKEN, transport=cap))
    mail.send(mail.pilot_password_changed("MixedCase@Example.TEST"))
    assert json.loads(cap.calls[0]["data"].decode("utf-8"))["To"] == "mixedcase@example.test"


def _portal_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(conn)
    org = conn.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    auth.create_user(conn, organization_id=org, email="user@example.test", password="Passw0rd!x",
                     role_names=["admin"], must_change_password=False)
    return conn


def test_identity_flow_fail_closed_when_postmark_delivery_fails(monkeypatch):
    """A Postmark outage must not enumerate accounts or leave a usable token."""
    conn = _portal_conn()
    cap = _Capture(exc=OSError("postmark down"))
    monkeypatch.setattr(mail, "get_mailer", lambda: mail.PostmarkMailer(FAKE_TOKEN, transport=cap))
    out = auth.request_password_reset(conn, "user@example.test", purpose=tokens.PORTAL_RESET)
    assert out == auth.GENERIC_CHECK_EMAIL  # generic, same as a nonexistent account
    live = conn.execute("SELECT COUNT(*) FROM auth_tokens WHERE purpose=? AND consumed_at IS NULL "
                        "AND revoked_at IS NULL", (tokens.PORTAL_RESET,)).fetchone()[0]
    assert live == 0  # failed-delivery token revoked, cannot be used later
    rows = [json.loads(r[0]) for r in conn.execute(
        "SELECT details_json FROM security_audit_log WHERE event_type='password_reset_requested'")]
    assert any(r.get("result") == "mail_unavailable" for r in rows)
    dump = json.dumps(rows)
    assert FAKE_TOKEN not in dump and "#t=" not in dump and "token" not in {k for r in rows for k in r}


def test_identity_flow_succeeds_through_postmark(monkeypatch):
    conn = _portal_conn()
    cap = _Capture(status=200)
    monkeypatch.setattr(mail, "get_mailer", lambda: mail.PostmarkMailer(FAKE_TOKEN, transport=cap))
    out = auth.request_password_reset(conn, "user@example.test", purpose=tokens.PORTAL_RESET)
    assert out == auth.GENERIC_CHECK_EMAIL
    assert len(cap.calls) == 1
    payload = json.loads(cap.calls[0]["data"].decode("utf-8"))
    assert payload["To"] == "user@example.test" and payload["From"] == "CorridorIQ <noreply@corridoriq.pro>"
    # the live token's raw value is delivered only inside the emailed link, never stored
    assert "#t=" in payload["TextBody"]
    live = conn.execute("SELECT token_hash FROM auth_tokens WHERE purpose=? AND consumed_at IS NULL "
                        "AND revoked_at IS NULL", (tokens.PORTAL_RESET,)).fetchall()
    assert len(live) == 1
    raw = payload["TextBody"].split("#t=", 1)[1].split()[0]
    assert live[0]["token_hash"] == tokens.hash_token(raw)  # only the hash is stored
