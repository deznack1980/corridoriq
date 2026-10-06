"""Purpose-bound, expiring, single-use authentication tokens.

Used for contractor signup verification, legacy email verification, owner
invitations and password reset in both identity stores (portal database and
pilot platform database — both carry the ``auth_tokens`` table).

Guarantees
----------
* Raw tokens are 256 bits of ``secrets`` randomness and are never stored —
  only their SHA-256 hash. The raw value exists once, inside the delivered
  link, and is never logged.
* A token is bound to a purpose and (depending on purpose) to a user id, a
  normalized email and/or the password state at issue time. Any mismatch
  fails closed.
* Consumption is a single conditional UPDATE: at most one caller can ever
  consume a given token, even under concurrency.
* Issuing a new token for the same subject/purpose revokes the outstanding
  ones, so stale links cannot be replayed after a replacement was requested.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

# Purposes (pilot platform store)
PILOT_SIGNUP = "pilot_signup"
PILOT_VERIFY = "pilot_email_verify"
PILOT_RESET = "pilot_password_reset"
# Purposes (portal store)
PORTAL_INVITE = "portal_owner_invite"
PORTAL_RESET = "portal_password_reset"

PURPOSES = frozenset({PILOT_SIGNUP, PILOT_VERIFY, PILOT_RESET, PORTAL_INVITE, PORTAL_RESET})

# Lifetimes
SIGNUP_TTL = timedelta(hours=24)
VERIFY_TTL = timedelta(hours=24)
INVITE_TTL = timedelta(hours=72)
RESET_TTL = timedelta(minutes=30)

_RAW_BYTES = 32  # 256 bits
_MAX_RAW_LEN = 128


class TokenError(Exception):
    """Generic token failure. The message never says *why* (no enumeration)."""

    def __init__(self, message: str = "invalid or expired token"):
        super().__init__(message)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def normalize_email(email) -> str:
    return (email or "").strip().lower()


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def password_marker(password_hash) -> str | None:
    """Binds a reset token to the password that was current when it was issued."""
    if not password_hash:
        return None
    return hashlib.sha256(str(password_hash).encode("utf-8")).hexdigest()


def _well_formed(raw) -> bool:
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_RAW_LEN:
        return False
    return all(ch.isalnum() or ch in "-_" for ch in raw)


def revoke(conn: sqlite3.Connection, *, purpose: str, user_id=None, email=None) -> int:
    """Revoke every outstanding token for a subject (user id and/or email)."""
    clauses, params = ["purpose=?", "consumed_at IS NULL", "revoked_at IS NULL"], [purpose]
    subj = []
    if user_id is not None:
        subj.append("user_id=?")
        params.append(user_id)
    if email:
        subj.append("email=?")
        params.append(normalize_email(email))
    if not subj:
        return 0
    clauses.append("(" + " OR ".join(subj) + ")")
    cur = conn.execute(f"UPDATE auth_tokens SET revoked_at=? WHERE {' AND '.join(clauses)}",
                       [_iso(_now()), *params])
    return cur.rowcount


def issue(conn: sqlite3.Connection, *, purpose: str, ttl: timedelta, user_id=None, email=None,
          password_marker: str | None = None, context: dict | None = None, ip=None) -> tuple[str, str]:
    """Create a token. Returns ``(raw_token, expires_at)``. Caller must commit."""
    if purpose not in PURPOSES:
        raise ValueError("unknown token purpose")
    if user_id is None and not email:
        raise ValueError("a token must be bound to a user or an email")
    revoke(conn, purpose=purpose, user_id=user_id, email=email)
    raw = secrets.token_urlsafe(_RAW_BYTES)
    now = _now()
    expires = _iso(now + ttl)
    conn.execute(
        "INSERT INTO auth_tokens (token_hash, purpose, user_id, email, password_marker, context_json, "
        "created_at, expires_at, created_ip) VALUES (?,?,?,?,?,?,?,?,?)",
        (hash_token(raw), purpose, user_id, normalize_email(email) if email else None, password_marker,
         json.dumps(context or {}, sort_keys=True), _iso(now), expires, ip))
    return raw, expires


def _matches(row, *, user_id, email, password_marker) -> bool:
    if user_id is not None and row["user_id"] != user_id:
        return False
    if email is not None and (row["email"] or "") != normalize_email(email):
        return False
    if password_marker is not None:
        if not row["password_marker"] or not hmac.compare_digest(row["password_marker"], password_marker):
            return False
    return True


def inspect(conn: sqlite3.Connection, raw, *, purpose: str, user_id=None, email=None) -> sqlite3.Row | None:
    """Read-only lookup. Returns the live token row or None. Never mutates."""
    if not _well_formed(raw):
        return None
    row = conn.execute(
        "SELECT * FROM auth_tokens WHERE token_hash=? AND purpose=? AND consumed_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > ?", (hash_token(raw), purpose, _iso(_now()))).fetchone()
    if row is None or not _matches(row, user_id=user_id, email=email, password_marker=None):
        return None
    return row


def consume(conn: sqlite3.Connection, raw, *, purpose: str, user_id=None, email=None,
            password_marker: str | None = None) -> sqlite3.Row:
    """Atomically consume a live token or raise TokenError.

    The conditional UPDATE is the single point of consumption. If the token
    turns out to be bound to a different subject it is still burned (never
    returned for a second attempt) and TokenError is raised; the caller's
    transaction should roll back any partial state.
    """
    if not _well_formed(raw):
        raise TokenError()
    now = _iso(_now())
    h = hash_token(raw)
    cur = conn.execute(
        "UPDATE auth_tokens SET consumed_at=? WHERE token_hash=? AND purpose=? AND consumed_at IS NULL "
        "AND revoked_at IS NULL AND expires_at > ?", (now, h, purpose, now))
    if cur.rowcount != 1:
        raise TokenError()
    row = conn.execute("SELECT * FROM auth_tokens WHERE token_hash=?", (h,)).fetchone()
    if not _matches(row, user_id=user_id, email=email, password_marker=password_marker):
        raise TokenError()
    return row


def context_of(row) -> dict:
    try:
        data = json.loads(row["context_json"] or "{}")
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def purge_expired(conn: sqlite3.Connection, *, older_than: timedelta = timedelta(days=30)) -> int:
    cutoff = _iso(_now() - older_than)
    return conn.execute("DELETE FROM auth_tokens WHERE expires_at < ?", (cutoff,)).rowcount


class transaction:
    """One SQLite transaction for consume + identity mutation.

    ``consume()`` does not commit. Callers must use this (or equivalent) so a
    later mutation failure rolls back the consume. Nested helpers must not
    commit on their own.
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def __enter__(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            # A DML statement already opened an implicit transaction.
            pass
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.conn.commit()
        else:
            self.conn.rollback()
        return False
