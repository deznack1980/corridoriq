"""Transactional mail for onboarding.

Development and tests write messages to an in-memory outbox. Optional SMTP
is used only when host, user, and password are set in the environment —
this module never invents a paid provider and never logs credentials.

Raw verification/reset tokens appear only in the in-memory message body
(needed so tests can follow the link). Disk outbox copies are redacted.
The security audit log never stores tokens.
"""

from __future__ import annotations

import json
import re
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from pipeline.config import settings

OUTBOX: list[dict] = []
LAST_SMTP: dict = {"status": "unconfigured", "error": None}

_TOKEN_RE = re.compile(r"token=[A-Za-z0-9_\-]+")
_SECRET_RE = re.compile(r"(password|passwd|secret|token)=([^\s]+)", re.I)


def clear_outbox() -> None:
    OUTBOX.clear()
    LAST_SMTP["status"] = "unconfigured" if not smtp_configured() else "idle"
    LAST_SMTP["error"] = None


def smtp_configured() -> bool:
    return bool(
        (getattr(settings, "SMTP_HOST", "") or "").strip()
        and (getattr(settings, "SMTP_USER", "") or "").strip()
        and (getattr(settings, "SMTP_PASSWORD", "") or "").strip()
    )


def diagnose() -> dict:
    """Operator-safe status. Never includes credentials or raw tokens."""
    return {
        "smtp_configured": smtp_configured(),
        "host_present": bool((getattr(settings, "SMTP_HOST", "") or "").strip()),
        "user_present": bool((getattr(settings, "SMTP_USER", "") or "").strip()),
        "password_present": bool((getattr(settings, "SMTP_PASSWORD", "") or "").strip()),
        "port": int(getattr(settings, "SMTP_PORT", 587) or 587),
        "use_ssl": bool(getattr(settings, "SMTP_USE_SSL", False)),
        "from_set": bool((getattr(settings, "SMTP_FROM", "") or "").strip()),
        "public_url": getattr(settings, "PUBLIC_APP_URL", ""),
        "outbox_dir_set": bool((getattr(settings, "MAIL_OUTBOX_DIR", "") or "").strip()),
        "last_smtp_status": LAST_SMTP.get("status"),
        "delivery_mode": "smtp" if smtp_configured() else "outbox",
    }


def redact_text(text: str) -> str:
    cleaned = _TOKEN_RE.sub("token=[redacted]", text or "")
    return _SECRET_RE.sub(r"\1=[redacted]", cleaned)


def send_mail(*, to: str, subject: str, text: str, purpose: str,
              user_id: int | None = None) -> dict:
    record = {
        "to": to,
        "subject": subject,
        "text": text,
        "purpose": purpose,
        "user_id": user_id,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "delivery": "outbox",
    }
    OUTBOX.append(record)
    _write_outbox_file(record)
    if smtp_configured():
        if _try_smtp(record):
            record["delivery"] = "smtp"
        else:
            record["delivery"] = "smtp_failed"
    return record


def _write_outbox_file(record: dict) -> None:
    directory = getattr(settings, "MAIL_OUTBOX_DIR", "") or ""
    if not directory:
        return
    path = Path(directory)
    try:
        path.mkdir(parents=True, exist_ok=True)
        name = f"{record['sent_at'].replace(':', '')}-{record['purpose']}-{len(OUTBOX)}.json"
        safe = {
            "to": record["to"],
            "subject": record["subject"],
            "text": redact_text(record["text"]),
            "purpose": record["purpose"],
            "user_id": record["user_id"],
            "sent_at": record["sent_at"],
            "delivery": record.get("delivery"),
        }
        (path / name).write_text(json.dumps(safe, indent=2), encoding="utf-8")
    except OSError:
        pass


def _sanitize_smtp_error(exc: Exception) -> str:
    return redact_text(type(exc).__name__)


def _try_smtp(record: dict) -> bool:
    host = (getattr(settings, "SMTP_HOST", "") or "").strip()
    user = (getattr(settings, "SMTP_USER", "") or "").strip()
    password = getattr(settings, "SMTP_PASSWORD", "") or ""
    port = int(getattr(settings, "SMTP_PORT", 587) or 587)
    use_ssl = bool(getattr(settings, "SMTP_USE_SSL", False))
    timeout = int(getattr(settings, "SMTP_TIMEOUT_SECONDS", 10) or 10)
    msg = EmailMessage()
    msg["Subject"] = record["subject"]
    msg["From"] = settings.SMTP_FROM
    msg["To"] = record["to"]
    msg.set_content(record["text"])
    try:
        factory = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
        with factory(host, port, timeout=timeout) as smtp:
            if not use_ssl:
                smtp.starttls()
            smtp.login(user, password)
            smtp.send_message(msg)
        LAST_SMTP["status"] = "sent"
        LAST_SMTP["error"] = None
        return True
    except Exception as exc:
        LAST_SMTP["status"] = "failed"
        LAST_SMTP["error"] = _sanitize_smtp_error(exc)
        return False


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


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="CorridorIQ mail diagnose (no secrets).")
    parser.add_argument("command", nargs="?", default="diagnose", choices=("diagnose",))
    args = parser.parse_args(argv)
    if args.command == "diagnose":
        print(json.dumps(diagnose(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
