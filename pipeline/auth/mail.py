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

import hashlib
import json
import os
import secrets
import sys
import threading
import urllib.error
import urllib.request
from contextvars import ContextVar
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

# Numeric Postmark codes used only to label a log line. The provider's Message
# text is never stored or printed.
POSTMARK_CATEGORIES = {
    10: "bad_token",
    300: "invalid_request",
    400: "sender_not_found",
    401: "sender_unconfirmed",
    405: "sending_not_allowed",
    406: "recipient_inactive",
    412: "account_pending",
    422: "invalid_stream",
}

_REQUEST_ID: ContextVar[str] = ContextVar("corridoriq_mail_request_id", default="")


class MailNotConfigured(Exception):
    pass


class MailSendError(Exception):
    """Delivery failed. The message never contains the token, recipient body,
    or provider response body — only a safe, generic reason."""

    def __init__(self, message: str, *, http_status: int | None = None,
                 error_code: int | None = None, category: str | None = None,
                 stream: str | None = None, token_fp: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.error_code = error_code
        self.category = category
        self.stream = stream
        self.token_fp = token_fp


@dataclass
class SendResult:
    http_status: int | None = None
    error_code: int | None = None
    message_id: str | None = None
    stream: str | None = None
    token_fp: str | None = None
    category: str = "accepted"


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
        # Read the body so ErrorCode can be logged. The text itself is never logged.
        try:
            return exc.code, exc.read()
        except Exception:
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

    def send(self, message: "Message") -> SendResult:
        data = json.dumps(self.build_payload(message)).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Postmark-Server-Token": self._token,
        }
        fingerprint = token_fingerprint(self._token)
        try:
            status, body = self._transport(POSTMARK_API_URL, data, headers)
        except Exception as exc:
            # Suppress the cause chain so no response body or header can leak.
            category = "timeout" if isinstance(exc, TimeoutError) else "network"
            raise MailSendError(
                f"postmark request failed: {type(exc).__name__}",
                category=category, stream=self._stream, token_fp=fingerprint,
            ) from None
        parsed = _safe_postmark_fields(body)
        if not (200 <= int(status) < 300):
            code = parsed.get("error_code")
            raise MailSendError(
                f"postmark send rejected (HTTP {int(status)})",
                http_status=int(status), error_code=code,
                category=_failure_category(int(status), code),
                stream=self._stream, token_fp=fingerprint,
            )
        return SendResult(
            http_status=int(status), error_code=parsed.get("error_code"),
            message_id=parsed.get("message_id"), stream=self._stream,
            token_fp=fingerprint, category="accepted",
        )


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


def _safe_postmark_fields(body) -> dict:
    """ErrorCode and MessageID only. Postmark's Message text can name a recipient."""
    try:
        payload = json.loads(body or b"{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    out = {}
    code = payload.get("ErrorCode")
    if isinstance(code, int) and not isinstance(code, bool):
        out["error_code"] = code
    message_id = payload.get("MessageID")
    if isinstance(message_id, str) and message_id and all(ch.isalnum() or ch == "-" for ch in message_id):
        out["message_id"] = message_id[:64]
    return out


def _failure_category(http_status: int, error_code: int | None) -> str:
    if error_code in POSTMARK_CATEGORIES:
        return POSTMARK_CATEGORIES[error_code]
    if http_status in (400, 401, 422):
        return f"http_{http_status}"
    return "rejected"


def token_fingerprint(token: str) -> str:
    """Short hash so two server credentials can be compared. Not reversible."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def begin_request() -> str:
    request_id = secrets.token_hex(4)
    _REQUEST_ID.set(request_id)
    return request_id


def _sender_domain() -> str:
    raw = (os.environ.get(ENV_MAIL_FROM) or DEFAULT_FROM).strip()
    address = raw.split("<")[-1].strip(" >")
    if "@" not in address:
        return "-"
    return address.rsplit("@", 1)[-1]


def _field(value) -> str:
    """One log token. Drops newlines and characters that could carry an address."""
    text = "-" if value is None or value == "" else str(value)
    cleaned = "".join(ch for ch in text if ch.isalnum() or ch in " ._:-(),/")
    return " ".join(cleaned.split()) or "-"


def _emit(event: str, **fields) -> None:
    fields.setdefault("ts", datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"))
    fields.setdefault("request_id", _REQUEST_ID.get() or "-")
    parts = [event]
    for key, value in fields.items():
        parts.append(f"{key}={_field(value)}")
    print(" ".join(parts), file=sys.stdout, flush=True)


def note_rate_limit(scope: str) -> None:
    """A signup was refused before any provider call. No address and no IP."""
    _emit("MAIL_RATE_LIMITED", kind=PILOT_SIGNUP, scope=scope, window="10m", category="rate_limited")


def log_outcome(kind: str, exc: Exception | None = None, *, result: SendResult | None = None) -> None:
    """One stdout line per identity email. Railway keeps stdout as the service log.

    Never includes the recipient, the link, the API token, or the provider message text.
    """
    if exc is None:
        _emit(
            "MAIL_SENT", kind=kind, provider=provider_name(),
            http_status=None if result is None else result.http_status,
            error_code=None if result is None else result.error_code,
            message_id=None if result is None else result.message_id,
            stream=None if result is None else result.stream,
            token_fp=None if result is None else result.token_fp,
            from_domain=_sender_domain(),
            category="accepted" if result is None else result.category,
        )
        return
    reason = str(exc) if isinstance(exc, (MailSendError, MailNotConfigured)) else type(exc).__name__
    _emit(
        "MAIL_DELIVERY_FAILED", kind=kind, provider=provider_name(),
        http_status=getattr(exc, "http_status", None),
        error_code=getattr(exc, "error_code", None),
        category=getattr(exc, "category", None) or "failed",
        stream=getattr(exc, "stream", None) or (os.environ.get(ENV_POSTMARK_STREAM) or DEFAULT_STREAM).strip(),
        from_domain=_sender_domain(),
        token_fp=getattr(exc, "token_fp", None),
        reason=reason,
    )


def send(message: Message) -> None:
    message.to = message.to.strip().lower()
    if not _REQUEST_ID.get():
        begin_request()
    stream = (os.environ.get(ENV_POSTMARK_STREAM) or DEFAULT_STREAM).strip()
    token = (os.environ.get(ENV_POSTMARK_TOKEN) or "").strip()
    _emit(
        "MAIL_SEND_ATTEMPT", kind=message.kind, provider=provider_name(),
        stream=stream, from_domain=_sender_domain(),
        token_fp=token_fingerprint(token) if token else None,
    )
    try:
        result = get_mailer().send(message)
    except Exception as exc:
        log_outcome(message.kind, exc)
        raise
    log_outcome(message.kind, result=result if isinstance(result, SendResult) else None)


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
