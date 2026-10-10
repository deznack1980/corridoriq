"""Transactional mail for onboarding.

Development and tests write messages to an in-memory outbox and, when
configured, a local directory. Optional SMTP is used only when host and
credentials are set — this module never invents a paid provider.
The raw verification/reset token appears only in the message body, never
in the security audit log.
"""

from __future__ import annotations

import json
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from pipeline.config import settings

OUTBOX: list[dict] = []


def clear_outbox() -> None:
    OUTBOX.clear()


def send_mail(*, to: str, subject: str, text: str, purpose: str,
              user_id: int | None = None) -> dict:
    record = {
        "to": to,
        "subject": subject,
        "text": text,
        "purpose": purpose,
        "user_id": user_id,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    OUTBOX.append(record)
    _write_outbox_file(record)
    _try_smtp(record)
    return record


def _write_outbox_file(record: dict) -> None:
    directory = getattr(settings, "MAIL_OUTBOX_DIR", "") or ""
    if not directory:
        return
    path = Path(directory)
    try:
        path.mkdir(parents=True, exist_ok=True)
        name = f"{record['sent_at'].replace(':', '')}-{record['purpose']}-{len(OUTBOX)}.json"
        # Persist the delivered message. The token lives in `text` because
        # that is the email the user receives; it is not written to the DB.
        safe = {k: v for k, v in record.items()}
        (path / name).write_text(json.dumps(safe, indent=2), encoding="utf-8")
    except OSError:
        pass


def _try_smtp(record: dict) -> None:
    host = getattr(settings, "SMTP_HOST", "") or ""
    user = getattr(settings, "SMTP_USER", "") or ""
    password = getattr(settings, "SMTP_PASSWORD", "") or ""
    if not (host and user and password):
        return
    msg = EmailMessage()
    msg["Subject"] = record["subject"]
    msg["From"] = settings.SMTP_FROM
    msg["To"] = record["to"]
    msg.set_content(record["text"])
    try:
        with smtplib.SMTP(host, settings.SMTP_PORT, timeout=10) as smtp:
            smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
    except Exception:
        # Local outbox remains the durable copy. SMTP failure must not
        # roll back account creation.
        pass


def verification_email(to: str, raw_token: str, *, user_id: int | None = None) -> dict:
    url = f"{settings.PUBLIC_APP_URL}/verify-email.html?token={raw_token}"
    return send_mail(
        to=to,
        subject="Verify your CorridorIQ email",
        text=(
            "Welcome to CorridorIQ.\n\n"
            "Confirm this email address to activate protected features "
            "(live RFQs, private quotations, and sensitive business information).\n\n"
            f"Verification link (expires in {settings.EMAIL_VERIFY_TTL_HOURS} hours):\n"
            f"{url}\n\n"
            "If you did not create an account, you can ignore this message.\n"
        ),
        purpose="email_verification",
        user_id=user_id,
    )


def already_registered_email(to: str, *, user_id: int | None = None) -> dict:
    return send_mail(
        to=to,
        subject="CorridorIQ account reminder",
        text=(
            "Someone tried to register a CorridorIQ account with this email.\n\n"
            "If that was you, sign in or reset your password:\n"
            f"{settings.PUBLIC_APP_URL}/login.html\n"
            f"{settings.PUBLIC_APP_URL}/reset-password.html\n\n"
            "If you did not try to register, you can ignore this message.\n"
        ),
        purpose="already_registered",
        user_id=user_id,
    )


def password_reset_email(to: str, raw_token: str, *, user_id: int | None = None) -> dict:
    url = f"{settings.PUBLIC_APP_URL}/reset-password.html?token={raw_token}"
    return send_mail(
        to=to,
        subject="Reset your CorridorIQ password",
        text=(
            "A password reset was requested for this CorridorIQ account.\n\n"
            f"Reset link (expires in {settings.PASSWORD_RESET_TTL_HOURS} hour):\n"
            f"{url}\n\n"
            "If you did not request this, you can ignore this message.\n"
        ),
        purpose="password_reset",
        user_id=user_id,
    )
