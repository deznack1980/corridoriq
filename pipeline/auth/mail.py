"""Transactional email abstraction for identity flows.

Two adapters exist: an in-memory development/test ``sink`` and a ``postmark``
transport for real delivery. Messages are built here (subject, text body,
link); an adapter only has to implement ``send``. The adapter transports the
message — it holds no CorridorIQ security logic (token generation, hashing,
expiry and fail-closed behavior all stay in the auth layer).

Links carry one-time tokens in the URL *fragment* (``#t=...``) so they never
reach server logs or Referer headers when the page is opened. Raw tokens and
the Postmark server token are never logged and never placed in an exception
message.
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pipeline.config import settings

ENV_PROVIDER = "CORRIDORIQ_EMAIL_PROVIDER"
ENV_PUBLIC_BASE_URL = "CORRIDORIQ_PUBLIC_BASE_URL"
ENV_POSTMARK_TOKEN = "POSTMARK_SERVER_TOKEN"
ENV_MAIL_FROM = "CORRIDORIQ_MAIL_FROM"
ENV_POSTMARK_STREAM = "CORRIDORIQ_POSTMARK_STREAM"
SINK = "sink"
POSTMARK = "postmark"

# Transactional sender identity (overridable via ENV_MAIL_FROM). The sending
# domain must already be authenticated with the provider (SPF/DKIM/DMARC).
DEFAULT_FROM = "CorridorIQ <noreply@corridoriq.pro>"
# Postmark's default transactional message stream.
DEFAULT_STREAM = "outbound"
POSTMARK_API_URL = "https://api.postmarkapp.com/email"

# Message kinds
PILOT_SIGNUP = "pilot_signup_verification"
PILOT_VERIFY = "pilot_email_verification"
PILOT_RESET = "pilot_password_reset"
PILOT_PASSWORD_CHANGED = "pilot_password_changed"
PORTAL_INVITE = "portal_owner_invitation"
PORTAL_RESET = "portal_password_reset"
PORTAL_PASSWORD_CHANGED = "portal_password_changed"


class MailNotConfigured(Exception):
    pass


class MailSendError(Exception):
    """Delivery failed. The message never contains the token, recipient body,
    or provider response body — only a safe, generic reason."""
    pass


@dataclass
class Message:
    to: str
    subject: str
    text: str
    kind: str
    link: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"))

    def redacted(self) -> dict:
        """Safe for logs/health: no body, no link (links contain tokens)."""
        return {"to": self.to, "kind": self.kind, "subject": self.subject, "created_at": self.created_at}


class DevSinkMailer:
    """Keeps messages in memory for tests and local development. Never logs them."""

    name = SINK

    def __init__(self):
        self._lock = threading.Lock()
        self._messages: list[Message] = []

    def send(self, message: Message) -> None:
        with self._lock:
            self._messages.append(message)

    def outbox(self, to: str | None = None, kind: str | None = None) -> list[Message]:
        with self._lock:
            items = list(self._messages)
        if to is not None:
            items = [m for m in items if m.to == to.strip().lower()]
        if kind is not None:
            items = [m for m in items if m.kind == kind]
        return items

    def clear(self) -> None:
        with self._lock:
            self._messages.clear()


_sink = DevSinkMailer()


def _default_transport(url: str, data: bytes, headers: dict) -> tuple[int, bytes]:  # pragma: no cover - network
    """POST to Postmark over HTTPS. Isolated so tests inject a fake transport."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        return exc.code, b""


class PostmarkMailer:
    """Sends a :class:`Message` through Postmark's transactional API.

    The server token is read from the environment, sent only in the
    ``X-Postmark-Server-Token`` request header, and never logged or echoed in
    an exception. Delivery failures raise :class:`MailSendError`, which the
    identity flows already treat as fail-closed (no delivery claim).
    """

    name = POSTMARK

    def __init__(self, token: str, *, sender: str | None = None,
                 stream: str | None = None, transport=None):
        self._token = token
        self._from = (sender or os.environ.get(ENV_MAIL_FROM) or DEFAULT_FROM).strip()
        self._stream = (stream or os.environ.get(ENV_POSTMARK_STREAM) or DEFAULT_STREAM).strip()
        self._transport = transport or _default_transport

    def build_payload(self, message: "Message") -> dict:
        """The exact JSON Postmark receives. No token, no tracking, text-only."""
        return {
            "From": self._from,
            "To": message.to,
            "Subject": message.subject,
            "TextBody": message.text,
            "MessageStream": self._stream,
        }

    def send(self, message: "Message") -> None:
        data = json.dumps(self.build_payload(message)).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self._token,
        }
        try:
            status, _body = self._transport(POSTMARK_API_URL, data, headers)
        except Exception as exc:
            # Suppress the cause chain so no response body/header can leak.
            raise MailSendError(f"postmark request failed: {type(exc).__name__}") from None
        if not (200 <= int(status) < 300):
            raise MailSendError(f"postmark send rejected (HTTP {int(status)})")


def provider_name() -> str:
    return (os.environ.get(ENV_PROVIDER) or SINK).strip().lower()


def get_mailer():
    """Return the configured adapter, or fail closed.

    Production must never "deliver" to the in-memory sink, and an unknown
    provider name or a Postmark selection without a token both fail closed so
    a live process cannot silently drop identity mail.
    """
    name = provider_name()
    if name == SINK:
        if (os.environ.get("CORRIDORIQ_ENV") or "") == "production":
            raise MailNotConfigured("production identity mail is not configured")
        return _sink
    if name == POSTMARK:
        token = (os.environ.get(ENV_POSTMARK_TOKEN) or "").strip()
        if not token:
            raise MailNotConfigured("postmark token is not configured")
        return PostmarkMailer(token)
    # Deliberately unsupported until a provider is chosen and configured
    # (SPF/DKIM/DMARC, sender identity, bounce handling). Fail closed.
    raise MailNotConfigured(f"email provider {name!r} is not configured on this server")


def sink() -> DevSinkMailer:
    return _sink


def public_base_url() -> str:
    base = (os.environ.get(ENV_PUBLIC_BASE_URL) or "").strip().rstrip("/")
    if base:
        return base
    return f"http://127.0.0.1:{settings.SALES_API_PORT}"


def token_link(page: str, mode: str, raw_token: str) -> str:
    return f"{public_base_url()}/{page}?mode={mode}#t={raw_token}"


def _hours(expires_at: str) -> str:
    return expires_at.replace("T", " ")


def send(message: Message) -> None:
    message.to = message.to.strip().lower()
    get_mailer().send(message)


# ---- message builders (plain text, no tracking, no marketing) ---------------

def pilot_signup_verification(to: str, link: str, expires_at: str) -> Message:
    return Message(
        to=to, kind=PILOT_SIGNUP, link=link,
        subject="Confirm your email to create your CorridorIQ account",
        text=(f"Open this link to confirm your email and finish creating your free CorridorIQ account:\n\n{link}\n\n"
              f"The link expires at {_hours(expires_at)} UTC and can be used once. "
              "If you did not request this, you can ignore this message."))


def pilot_email_verification(to: str, link: str, expires_at: str) -> Message:
    return Message(
        to=to, kind=PILOT_VERIFY, link=link,
        subject="Verify your CorridorIQ email address",
        text=(f"Open this link to verify that you own this email address:\n\n{link}\n\n"
              f"The link expires at {_hours(expires_at)} UTC and can be used once."))


def pilot_password_reset(to: str, link: str, expires_at: str) -> Message:
    return Message(
        to=to, kind=PILOT_RESET, link=link,
        subject="Reset your CorridorIQ password",
        text=(f"Open this link to choose a new password:\n\n{link}\n\n"
              f"The link expires at {_hours(expires_at)} UTC and can be used once. "
              "If you did not request a password reset, you can ignore this message; your password has not changed."))


def pilot_password_changed(to: str) -> Message:
    return Message(
        to=to, kind=PILOT_PASSWORD_CHANGED,
        subject="Your CorridorIQ password was changed",
        text=("The password for your CorridorIQ account was just changed and other sessions were signed out. "
              "If you did not do this, request a password reset immediately."))


def portal_owner_invitation(to: str, link: str, expires_at: str, company_name: str) -> Message:
    return Message(
        to=to, kind=PORTAL_INVITE, link=link,
        subject="Your CorridorIQ account is ready to set up",
        text=(f"A CorridorIQ account for {company_name} has been created with you as the account owner. "
              f"Open this link to confirm your email and choose your password:\n\n{link}\n\n"
              f"The link expires at {_hours(expires_at)} UTC and can be used once."))


def portal_password_reset(to: str, link: str, expires_at: str) -> Message:
    return Message(
        to=to, kind=PORTAL_RESET, link=link,
        subject="Reset your CorridorIQ password",
        text=(f"Open this link to choose a new password:\n\n{link}\n\n"
              f"The link expires at {_hours(expires_at)} UTC and can be used once. "
              "If you did not request a password reset, you can ignore this message; your password has not changed."))


def portal_password_changed(to: str) -> Message:
    return Message(
        to=to, kind=PORTAL_PASSWORD_CHANGED,
        subject="Your CorridorIQ password was changed",
        text=("The password for your CorridorIQ account was just changed and other sessions were signed out. "
              "If you did not do this, contact your administrator immediately."))
