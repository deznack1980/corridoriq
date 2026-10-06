"""Authentication service: login, lockout, sessions, password changes, and the
security audit log. Failed logins never reveal whether an account exists."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from pipeline.auth import passwords, sessions
from pipeline.auth.rbac import load_user_permissions, load_user_roles
from pipeline.config import settings

# Keys that must never appear inside security_audit_log.details_json.
_FORBIDDEN_DETAIL_KEYS = {
    "password", "new_password", "old_password", "password_hash", "token",
    "session", "secret", "api_key", "authorization", "token_hash",
    "raw_token", "reset_token", "invitation_token",
}

GENERIC_LOGIN_ERROR = "Invalid email or password."


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _row_get(row, key, default=None):
    if row is None:
        return default
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def email_is_verified(row) -> bool:
    """True only when a verification timestamp exists and matches current email."""
    if row is None:
        return False
    at = _row_get(row, "email_verified_at")
    addr = normalize_email(_row_get(row, "email_verified_address") or "")
    current = normalize_email(_row_get(row, "normalized_email") or _row_get(row, "email") or "")
    return bool(at) and bool(addr) and addr == current


def mark_email_verified(conn: sqlite3.Connection, user_id: int, email: str) -> None:
    now = _iso(_now())
    conn.execute(
        "UPDATE users SET email_verified_at=?, email_verified_address=?, updated_at=? WHERE id=?",
        (now, normalize_email(email), now, user_id),
    )


def clear_email_verification(conn: sqlite3.Connection, user_id: int) -> None:
    conn.execute(
        "UPDATE users SET email_verified_at=NULL, email_verified_address=NULL, updated_at=? WHERE id=?",
        (_iso(_now()), user_id),
    )


def apply_email_change(conn: sqlite3.Connection, user_id: int, new_email, *,
                       actor_id: int | None = None, ip=None, ua=None,
                       commit: bool = True) -> str:
    """Operator-supported email change: clear verification and revoke tokens."""
    from pipeline.auth import tokens

    norm = normalize_email(new_email)
    if not norm or "@" not in norm or " " in norm:
        raise ValueError("A valid email is required.")
    existing = _get_user_row(conn, norm)
    if existing is not None and existing["id"] != user_id:
        raise ValueError("A user with that email already exists.")
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise ValueError("user not found")
    old = row["normalized_email"]
    if old != norm:
        conn.execute(
            "UPDATE users SET email=?, normalized_email=?, email_verified_at=NULL, "
            "email_verified_address=NULL, updated_at=? WHERE id=?",
            (str(new_email).strip(), norm, _iso(_now()), user_id),
        )
        for purpose in (tokens.PILOT_VERIFY, tokens.PILOT_RESET, tokens.PILOT_SIGNUP,
                        tokens.PORTAL_RESET, tokens.PORTAL_INVITE):
            tokens.revoke(conn, purpose=purpose, user_id=user_id, email=old)
            tokens.revoke(conn, purpose=purpose, user_id=user_id, email=norm)
        write_audit(conn, event_type="email_changed", success=True, user_id=actor_id or user_id,
                    organization_id=row["organization_id"], resource_type="user",
                    resource_id=user_id, ip_address=ip, user_agent=ua,
                    details={"from": old, "to": norm}, commit=False)
    if commit:
        conn.commit()
    return norm


def _sanitize_details(details: dict | None) -> str | None:
    if not details:
        return None
    clean = {k: v for k, v in details.items()
             if k.lower() not in _FORBIDDEN_DETAIL_KEYS}
    return json.dumps(clean, default=str)


def write_audit(conn: sqlite3.Connection, *, event_type: str,
                user_id: int | None = None, organization_id: int | None = None,
                resource_type: str | None = None, resource_id=None,
                action: str | None = None, success: bool | None = None,
                ip_address: str | None = None, user_agent: str | None = None,
                details: dict | None = None, commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO security_audit_log (user_id, organization_id, event_type, "
        "resource_type, resource_id, action, success, ip_address, user_agent, "
        "details_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (user_id, organization_id, event_type, resource_type,
         str(resource_id) if resource_id is not None else None, action,
         None if success is None else (1 if success else 0),
         ip_address, user_agent, _sanitize_details(details), _iso(_now())),
    )
    if commit:
        conn.commit()


def build_user_context(conn: sqlite3.Connection, user_row) -> dict:
    from pipeline.auth.rbac import dashboard_mode, default_landing_page

    roles = load_user_roles(conn, user_row["id"])
    permissions = load_user_permissions(conn, user_row["id"])
    org = conn.execute(
        "SELECT id, name, slug FROM organizations WHERE id=?",
        (user_row["organization_id"],),
    ).fetchone()
    return {
        "id": user_row["id"],
        "organization_id": user_row["organization_id"],
        "organization": {
            "id": org["id"],
            "name": org["name"],
            "slug": org["slug"],
        } if org else None,
        "email": user_row["email"],
        "display_name": user_row["display_name"] or user_row["email"],
        "first_name": user_row["first_name"],
        "last_name": user_row["last_name"],
        "must_change_password": bool(user_row["must_change_password"]),
        "is_active": bool(user_row["is_active"]),
        "email_verified": email_is_verified(user_row),
        "roles": roles,
        "permissions": permissions,
        "default_landing_page": default_landing_page(roles, permissions),
        "dashboard_mode": dashboard_mode(roles, permissions),
    }


def serialize_user_context(user: dict) -> dict:
    """JSON-safe user payload for /api/auth/me and login responses."""
    safe = dict(user)
    safe["permissions"] = sorted(user.get("permissions") or [])
    safe["roles"] = list(user.get("roles") or [])
    return safe


def _get_user_row(conn: sqlite3.Connection, normalized_email: str):
    return conn.execute(
        "SELECT * FROM users WHERE normalized_email=?", (normalized_email,)
    ).fetchone()


def login(conn: sqlite3.Connection, email: str, password: str, *,
          ip_address: str | None = None, user_agent: str | None = None):
    """Authenticate and open a session. Raises AuthError (generic) on failure."""
    norm = normalize_email(email)
    row = _get_user_row(conn, norm)
    now = _now()

    def _fail(reason: str):
        write_audit(conn, event_type="login_failure", success=False,
                    user_id=row["id"] if row else None,
                    organization_id=row["organization_id"] if row else None,
                    ip_address=ip_address, user_agent=user_agent,
                    details={"email": norm, "reason": reason})
        raise AuthError(GENERIC_LOGIN_ERROR)

    if row is None:
        # Do a dummy hash to keep timing similar and avoid user enumeration.
        passwords.verify_password(password, "scrypt$32768$8$1$00$00")
        _fail("no_such_user")

    # Locked? (parse defensively, but never let _fail be swallowed here)
    if row["locked_until"]:
        locked_until = None
        try:
            locked_until = datetime.fromisoformat(row["locked_until"])
        except Exception:
            locked_until = None
        if locked_until is not None and locked_until > now:
            _fail("locked")

    if not row["is_active"]:
        _fail("disabled")

    if not passwords.verify_password(password, row["password_hash"]):
        new_count = (row["failed_login_count"] or 0) + 1
        locked_until = None
        if new_count >= settings.AUTH_MAX_FAILED_LOGINS:
            locked_until = _iso(now + timedelta(minutes=settings.AUTH_LOCKOUT_MINUTES))
        conn.execute(
            "UPDATE users SET failed_login_count=?, locked_until=?, updated_at=? WHERE id=?",
            (new_count, locked_until, _iso(now), row["id"]),
        )
        conn.commit()
        if locked_until:
            write_audit(conn, event_type="account_locked", success=False,
                        user_id=row["id"], organization_id=row["organization_id"],
                        ip_address=ip_address, user_agent=user_agent,
                        details={"failed_login_count": new_count})
        _fail("bad_credentials")

    # Success — reset counters, optionally upgrade hash, open session.
    new_hash = None
    if passwords.needs_rehash(row["password_hash"]):
        new_hash = passwords.hash_password(password)
    conn.execute(
        "UPDATE users SET failed_login_count=0, locked_until=NULL, last_login_at=?, "
        "updated_at=?" + (", password_hash=?" if new_hash else "") + " WHERE id=?",
        ((_iso(now), _iso(now), new_hash, row["id"]) if new_hash
         else (_iso(now), _iso(now), row["id"])),
    )
    conn.commit()
    token = sessions.create_session(conn, row["id"], ip_address=ip_address,
                                    user_agent=user_agent)
    write_audit(conn, event_type="login_success", success=True, user_id=row["id"],
                organization_id=row["organization_id"], ip_address=ip_address,
                user_agent=user_agent)
    row = _get_user_row(conn, norm)
    return token, build_user_context(conn, row)


def get_current_user(conn: sqlite3.Connection, token: str | None):
    session = sessions.get_session(conn, token)
    if session is None:
        return None
    row = conn.execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
    if row is None or not row["is_active"]:
        return None
    return build_user_context(conn, row)


def logout(conn: sqlite3.Connection, token: str, *, user_id: int | None = None,
           ip_address: str | None = None, user_agent: str | None = None) -> None:
    sessions.revoke_session(conn, token)
    write_audit(conn, event_type="logout", success=True, user_id=user_id,
                ip_address=ip_address, user_agent=user_agent)


def change_password(conn: sqlite3.Connection, user_id: int, old_password: str,
                    new_password: str, *, ip_address: str | None = None,
                    user_agent: str | None = None,
                    keep_token: str | None = None) -> None:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise AuthError("user not found", status=404)
    if not passwords.verify_password(old_password, row["password_hash"]):
        write_audit(conn, event_type="password_changed", success=False,
                    user_id=user_id, organization_id=row["organization_id"],
                    ip_address=ip_address, user_agent=user_agent,
                    details={"reason": "bad_current_credentials"})
        raise AuthError("Current password is incorrect.", status=400)
    if len(new_password or "") < 8:
        raise AuthError("New password must be at least 8 characters.", status=400)
    new_hash = passwords.hash_password(new_password)
    conn.execute(
        "UPDATE users SET password_hash=?, must_change_password=0, updated_at=? WHERE id=?",
        (new_hash, _iso(_now()), user_id),
    )
    conn.commit()
    # Password change revokes all existing sessions.
    sessions.revoke_user_sessions(conn, user_id)
    from pipeline.auth import tokens
    tokens.revoke(conn, purpose=tokens.PORTAL_RESET, user_id=user_id, email=row["normalized_email"])
    tokens.revoke(conn, purpose=tokens.PILOT_RESET, user_id=user_id, email=row["normalized_email"])
    conn.commit()
    write_audit(conn, event_type="password_changed", success=True, user_id=user_id,
                organization_id=row["organization_id"], ip_address=ip_address,
                user_agent=user_agent,
                details={"required": bool(row["must_change_password"])})


def create_user(conn: sqlite3.Connection, *, organization_id: int, email: str,
                password: str, first_name: str = "", last_name: str = "",
                display_name: str | None = None, phone: str | None = None,
                role_names: list[str] | None = None,
                must_change_password: bool = True,
                created_by: int | None = None,
                commit: bool = True) -> int:
    """Create a user with a hashed password and role assignments."""
    norm = normalize_email(email)
    if not norm or "@" not in norm:
        raise ValueError("A valid email is required.")
    if _get_user_row(conn, norm) is not None:
        raise ValueError("A user with that email already exists.")
    now = _iso(_now())
    pw_hash = passwords.hash_password(password)
    display = display_name or (f"{first_name} {last_name}".strip() or email)
    cur = conn.execute(
        "INSERT INTO users (organization_id, email, normalized_email, password_hash, "
        "first_name, last_name, display_name, phone, is_active, must_change_password, "
        "failed_login_count, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,0,?,?)",
        (organization_id, email.strip(), norm, pw_hash, first_name, last_name,
         display, phone, 1 if must_change_password else 0, now, now),
    )
    user_id = cur.lastrowid
    for rname in (role_names or []):
        r = conn.execute("SELECT id FROM roles WHERE name=?", (rname,)).fetchone()
        if r is None:
            raise ValueError(f"Unknown role: {rname}")
        conn.execute(
            "INSERT OR IGNORE INTO user_roles (user_id, role_id, assigned_by, assigned_at) "
            "VALUES (?,?,?,?)",
            (user_id, r["id"], created_by, now),
        )
    write_audit(conn, event_type="admin_change", success=True, user_id=created_by,
                organization_id=organization_id, resource_type="user",
                resource_id=user_id, action="create_user",
                details={"email": norm, "roles": role_names or []}, commit=False)
    if commit:
        conn.commit()
    return user_id


GENERIC_CHECK_EMAIL = {"status": "check_email"}


def request_password_reset(conn: sqlite3.Connection, email, *, ip=None, ua=None,
                           purpose: str | None = None) -> dict:
    """Always return the same payload. Issue a reset token only when a user exists."""
    from pipeline.auth import mail, tokens

    purpose = purpose or tokens.PORTAL_RESET
    norm = normalize_email(email)
    row = _get_user_row(conn, norm) if norm else None
    if row is not None and row["is_active"]:
        raw, expires = tokens.issue(
            conn, purpose=purpose, ttl=tokens.RESET_TTL, user_id=row["id"], email=norm,
            password_marker=tokens.password_marker(row["password_hash"]), ip=ip)
        page = "contractor-account.html" if purpose == tokens.PILOT_RESET else "login.html"
        mode = "reset"
        link = mail.token_link(page, mode, raw)
        if purpose == tokens.PILOT_RESET:
            mail.send(mail.pilot_password_reset(norm, link, expires))
        else:
            mail.send(mail.portal_password_reset(norm, link, expires))
        write_audit(conn, event_type="password_reset_requested", success=True,
                    user_id=row["id"], organization_id=row["organization_id"],
                    ip_address=ip, user_agent=ua, details={"email": norm})
    else:
        write_audit(conn, event_type="password_reset_requested", success=True,
                    ip_address=ip, user_agent=ua, details={"email": norm, "result": "no_account"})
    return dict(GENERIC_CHECK_EMAIL)


def complete_password_reset(conn: sqlite3.Connection, raw_token, new_password, *,
                            purpose: str | None = None, ip=None, ua=None) -> None:
    """Set a new password. Does not create a session."""
    from pipeline.auth import mail, tokens

    purpose = purpose or tokens.PORTAL_RESET
    if len(new_password or "") < 8 or len(new_password or "") > 200:
        raise AuthError("New password must be at least 8 characters.", status=400)
    user = None
    try:
        with tokens.transaction(conn):
            preview = tokens.inspect(conn, raw_token, purpose=purpose)
            if preview is None:
                raise tokens.TokenError()
            user = conn.execute("SELECT * FROM users WHERE id=?", (preview["user_id"],)).fetchone()
            if user is None or not user["is_active"]:
                raise tokens.TokenError()
            marker = tokens.password_marker(user["password_hash"])
            tokens.consume(conn, raw_token, purpose=purpose, user_id=user["id"],
                           email=user["normalized_email"], password_marker=marker)
            new_hash = passwords.hash_password(new_password)
            conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=0, failed_login_count=0, "
                "locked_until=NULL, updated_at=? WHERE id=?",
                (new_hash, _iso(_now()), user["id"]),
            )
            sessions.revoke_user_sessions(conn, user["id"], commit=False)
            tokens.revoke(conn, purpose=purpose, user_id=user["id"], email=user["normalized_email"])
            write_audit(conn, event_type="password_reset_completed", success=True,
                        user_id=user["id"], organization_id=user["organization_id"],
                        ip_address=ip, user_agent=ua, details={"email": user["normalized_email"]},
                        commit=False)
    except tokens.TokenError as exc:
        write_audit(conn, event_type="password_reset_completed", success=False,
                    ip_address=ip, user_agent=ua, details={"reason": "invalid_token"})
        raise AuthError("invalid or expired token", status=400) from exc
    if user is not None:
        if purpose == tokens.PILOT_RESET:
            mail.send(mail.pilot_password_changed(user["normalized_email"]))
        else:
            mail.send(mail.portal_password_changed(user["normalized_email"]))
