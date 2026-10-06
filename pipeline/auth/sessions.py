"""Server-side session management. The browser only holds an opaque token
inside an HTTP-only cookie; all session state lives in the database so that
logout and password changes can revoke sessions immediately."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from pipeline.config import settings


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def create_session(conn: sqlite3.Connection, user_id: int, *,
                   ip_address: str | None = None,
                   user_agent: str | None = None,
                   commit: bool = True) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    expires = now + timedelta(hours=settings.SESSION_TTL_HOURS)
    conn.execute(
        "INSERT INTO sessions (token, user_id, created_at, expires_at, "
        "ip_address, user_agent) VALUES (?, ?, ?, ?, ?, ?)",
        (token, user_id, _iso(now), _iso(expires), ip_address, user_agent),
    )
    if commit:
        conn.commit()
    return token


def get_session(conn: sqlite3.Connection, token: str | None):
    """Return the sessions row if valid (exists, not revoked, not expired)."""
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM sessions WHERE token=?", (token,)
    ).fetchone()
    if row is None or row["revoked_at"] is not None:
        return None
    try:
        expires = datetime.fromisoformat(row["expires_at"])
    except Exception:
        return None
    if expires <= _now():
        return None
    return row


def revoke_session(conn: sqlite3.Connection, token: str, *, commit: bool = True) -> None:
    conn.execute(
        "UPDATE sessions SET revoked_at=? WHERE token=? AND revoked_at IS NULL",
        (_iso(_now()), token),
    )
    if commit:
        conn.commit()


def revoke_user_sessions(conn: sqlite3.Connection, user_id: int, *, commit: bool = True) -> int:
    """Revoke every active session for a user (used on password change/disable)."""
    cur = conn.execute(
        "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
        (_iso(_now()), user_id),
    )
    if commit:
        conn.commit()
    return cur.rowcount
