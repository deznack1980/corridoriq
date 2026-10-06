"""Transactional email abstraction for identity flows.

Only a development/test sink exists today. No production email provider is
configured, no DNS/M365/Postmark/SES wiring, and no real message leaves this
process. Messages are built here (subject, text body, link) so a future
provider only has to implement ``send``.

Links carry one-time tokens in the URL *fragment* (``#t=...``) so they never
reach server logs or Referer headers when the page is opened.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pipeline.config import settings

ENV_PROVIDER = "CORRIDORIQ_EMAIL_PROVIDER"
ENV_PUBLIC_BASE_URL = "CORRIDORIQ_PUBLIC_BASE_URL"
SINK = "sink"

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


def provider_name() -> str:
    return (os.environ.get(ENV_PROVIDER) or SINK).strip().lower()


def get_mailer():
    # Production has no identity provider yet. Refuse the sink and every
    # unsupported name so a live process cannot silently "deliver" in memory.
    if (os.environ.get("CORRIDORIQ_ENV") or "").strip().lower() == "production":
        raise MailNotConfigured("production identity mail is not configured")
    name = provider_name()
    if name == SINK:
        return _sink
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
