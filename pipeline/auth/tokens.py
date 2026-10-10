"""Hashed, single-use authentication tokens.

The raw token is returned once to the caller (and emailed). Only a SHA-256
hash is stored. Used tokens and expired tokens cannot be reused.
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from pipeline.config import settings

PURPOSE_EMAIL_VERIFICATION = "email_verification"
PURPOSE_PASSWORD_RESET = "password_reset"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def issue_token(conn: sqlite3.Connection, user_id: int, purpose: str,
                ttl_hours: float) -> str:
    """Create a single-use token. Returns the raw secret once."""
    raw = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(hours=ttl_hours)
    conn.execute(
        "INSERT INTO auth_tokens (user_id, purpose, token_hash, expires_at, created_at) "
        "VALUES (?,?,?,?,?)",
        (user_id, purpose, hash_token(raw), _iso(expires), _iso(now)),
    )
    conn.commit()
    return raw


def consume_token(conn: sqlite3.Connection, raw: str, purpose: str):
    """Return the token row after marking it used, or None if invalid/expired."""
    if not raw or not isinstance(raw, str):
        return None
    row = conn.execute(
        "SELECT * FROM auth_tokens WHERE token_hash=?", (hash_token(raw),)
    ).fetchone()
    if row is None or row["purpose"] != purpose or row["used_at"]:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except Exception:
        return None
    if expires <= _now():
        return None
    conn.execute(
        "UPDATE auth_tokens SET used_at=? WHERE id=?",
        (_iso(_now()), row["id"]),
    )
    conn.commit()
    return row


def latest_unexpired(conn: sqlite3.Connection, user_id: int, purpose: str):
    now = _iso(_now())
    return conn.execute(
        "SELECT * FROM auth_tokens WHERE user_id=? AND purpose=? "
        "AND used_at IS NULL AND expires_at > ? ORDER BY id DESC LIMIT 1",
        (user_id, purpose, now),
    ).fetchone()


def default_ttl(purpose: str) -> float:
    if purpose == PURPOSE_PASSWORD_RESET:
        return settings.PASSWORD_RESET_TTL_HOURS
    return settings.EMAIL_VERIFY_TTL_HOURS
