"""Self-service contractor and supplier registration, verification, and gates.

Employee accounts continue to be created only by administrators. These
helpers never assign the admin role. Protected operations are denied here
in the backend — hiding a button is not authorization.
"""

from __future__ import annotations

import re
import sqlite3

from pipeline.auth import mailer, tokens
from pipeline.auth.rbac import AuthzError, has_permission, require_permission
from pipeline.auth.seed import DEFAULT_ORG_SLUG
from pipeline.auth.service import (
    AuthError,
    _get_user_row,
    build_user_context,
    create_user,
    login,
    normalize_email,
    write_audit,
)
from pipeline.auth import passwords, sessions
from pipeline.config import settings

KIND_EMPLOYEE = "employee"
KIND_CONTRACTOR = "contractor"
KIND_SUPPLIER = "supplier"

STATE_PENDING_EMAIL = "PENDING_EMAIL_VERIFICATION"
STATE_PENDING_APPROVAL = "PENDING_SUPPLIER_APPROVAL"
STATE_ACTIVE = "ACTIVE"
STATE_SUSPENDED = "SUSPENDED"

GENERIC_REGISTER_ERROR = (
    "Could not create this account. If you already have one, sign in or reset your password."
)
GENERIC_RESET_ACK = "If an account exists for that email, a reset link is on its way."
GENERIC_RESEND_ACK = "If this account needs verification, a new link is on its way."
VERIFY_REQUIRED = "Email verification required to activate this feature."
SUPPLIER_APPROVAL_REQUIRED = (
    "Supplier access requires email verification and administrative approval."
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+().\- ]{7,25}$")
_NAME_RE = re.compile(r".*\S.*")

PROTECTED_CONTRACTOR = (
    "contractor.rfq.submit",
    "contractor.intelligence.view",
    "contractor.quotes.view",
)
PROTECTED_SUPPLIER = (
    "supplier.rfq.access",
    "supplier.quote.submit",
    "supplier.contacts.view",
    "supplier.commercial.view",
)

SUPPLIER_CATEGORIES = (
    "plumbing",
    "hvac",
    "electrical",
    "general_building",
    "mechanical",
    "other",
)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _user_cols(conn: sqlite3.Connection) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(users)")}


def _row_get(row, key, default=None):
    try:
        return row[key]
    except (IndexError, KeyError, TypeError):
        return default


def default_org_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT id FROM organizations WHERE slug=?", (DEFAULT_ORG_SLUG,)
    ).fetchone()
    if row is None:
        raise AuthError("Platform organization is not available.", status=503)
    return row["id"]


def account_kind(user: dict) -> str:
    return (user or {}).get("account_kind") or KIND_EMPLOYEE


def account_state(user: dict) -> str:
    return (user or {}).get("account_state") or STATE_ACTIVE


def is_email_verified(user: dict) -> bool:
    if not user:
        return False
    if user.get("email_verified"):
        return True
    if user.get("email_verified_at"):
        return True
    # Existing employee accounts remain fully usable without a verification loop.
    return account_kind(user) == KIND_EMPLOYEE


def is_suspended(user: dict) -> bool:
    return account_state(user) == STATE_SUSPENDED or not user.get("is_active", True)


def require_verified(user: dict, message: str = VERIFY_REQUIRED) -> None:
    if not is_email_verified(user):
        raise AuthzError(message, status=403)


def require_supplier_live(user: dict) -> None:
    if account_kind(user) != KIND_SUPPLIER:
        raise AuthzError("permission denied", status=403)
    require_verified(user, SUPPLIER_APPROVAL_REQUIRED)
    if account_state(user) != STATE_ACTIVE:
        raise AuthzError(SUPPLIER_APPROVAL_REQUIRED, status=403)


def require_contractor(user: dict) -> None:
    if account_kind(user) != KIND_CONTRACTOR and "contractor" not in (user.get("roles") or []):
        raise AuthzError("permission denied", status=403)


def require_not_suspended(user: dict) -> None:
    if is_suspended(user):
        raise AuthzError("account suspended", status=403)


def require_employee_workspace(user: dict) -> None:
    """Sales / admin / intelligence APIs stay employee-only."""
    kind = account_kind(user)
    if kind in (KIND_CONTRACTOR, KIND_SUPPLIER):
        raise AuthzError("permission denied", status=403)
    roles = set(user.get("roles") or [])
    if roles and roles <= {"contractor", "supplier"}:
        raise AuthzError("permission denied", status=403)


def _clean_name(value) -> str:
    return " ".join(str(value or "").split())[:120]


def _clean_business(value) -> str:
    return " ".join(str(value or "").split())[:200]


def _validate_email(email: str) -> str:
    norm = normalize_email(email)
    if not norm or len(norm) > 254 or not _EMAIL_RE.fullmatch(norm):
        raise AuthError("Enter a valid email address.", status=400)
    return norm


def _validate_password(password: str) -> str:
    if not isinstance(password, str) or len(password) < 8 or len(password) > 200:
        raise AuthError("Password must be at least 8 characters.", status=400)
    return password


def _validate_phone(phone, *, required: bool) -> str | None:
    cleaned = " ".join(str(phone or "").split()) or None
    if not cleaned:
        if required:
            raise AuthError("Business phone is required.", status=400)
        return None
    if not _PHONE_RE.fullmatch(cleaned):
        raise AuthError("Enter a valid business phone.", status=400)
    return cleaned


def _dummy_password_work() -> None:
    passwords.verify_password("not-the-password", "scrypt$32768$8$1$00$00")


def _issue_verification(conn, user_id: int, email: str) -> None:
    raw = tokens.issue_token(
        conn, user_id, tokens.PURPOSE_EMAIL_VERIFICATION,
        tokens.default_ttl(tokens.PURPOSE_EMAIL_VERIFICATION),
    )
    mailer.verification_email(email, raw, user_id=user_id)
    write_audit(conn, event_type="verification_sent", success=True, user_id=user_id,
                details={"purpose": "email_verification"})


def register_contractor(conn: sqlite3.Connection, body: dict, *,
                        ip=None, ua=None):
    name = _clean_name(body.get("name") or body.get("display_name"))
    business = _clean_business(body.get("business_name"))
    email = _validate_email(body.get("email", ""))
    password = _validate_password(body.get("password") or "")
    if not name or not _NAME_RE.fullmatch(name):
        raise AuthError("Name is required.", status=400)
    if not business:
        raise AuthError("Business name is required.", status=400)

    existing = _get_user_row(conn, email)
    if existing is not None:
        _dummy_password_work()
        mailer.already_registered_email(email, user_id=existing["id"])
        write_audit(conn, event_type="register_duplicate", success=False,
                    user_id=existing["id"], ip_address=ip, user_agent=ua,
                    details={"kind": KIND_CONTRACTOR})
        raise AuthError(GENERIC_REGISTER_ERROR, status=409)

    first, _, last = name.partition(" ")
    user_id = create_user(
        conn, organization_id=default_org_id(conn), email=email, password=password,
        first_name=first, last_name=last.strip(), display_name=name,
        role_names=["contractor"], must_change_password=False,
        account_kind=KIND_CONTRACTOR, account_state=STATE_ACTIVE,
        email_verified=False, business_name=business,
    )
    _issue_verification(conn, user_id, email)
    write_audit(conn, event_type="contractor_registered", success=True,
                user_id=user_id, ip_address=ip, user_agent=ua)
    token, user = login(conn, email, password, ip_address=ip, user_agent=ua)
    return token, user


def register_supplier(conn: sqlite3.Connection, body: dict, *,
                      ip=None, ua=None):
    contact = _clean_name(body.get("name") or body.get("contact_name"))
    business = _clean_business(body.get("business_name"))
    email = _validate_email(body.get("email") or body.get("business_email") or "")
    password = _validate_password(body.get("password") or "")
    phone = _validate_phone(body.get("phone") or body.get("business_phone"), required=True)
    category = str(body.get("business_category") or body.get("category") or "").strip().lower()
    if not contact:
        raise AuthError("Contact name is required.", status=400)
    if not business:
        raise AuthError("Business name is required.", status=400)
    if category not in SUPPLIER_CATEGORIES:
        raise AuthError("Select a valid business category.", status=400)

    existing = _get_user_row(conn, email)
    if existing is not None:
        _dummy_password_work()
        mailer.already_registered_email(email, user_id=existing["id"])
        write_audit(conn, event_type="register_duplicate", success=False,
                    user_id=existing["id"], ip_address=ip, user_agent=ua,
                    details={"kind": KIND_SUPPLIER})
        raise AuthError(GENERIC_REGISTER_ERROR, status=409)

    first, _, last = contact.partition(" ")
    user_id = create_user(
        conn, organization_id=default_org_id(conn), email=email, password=password,
        first_name=first, last_name=last.strip(), display_name=contact, phone=phone,
        role_names=["supplier"], must_change_password=False,
        account_kind=KIND_SUPPLIER, account_state=STATE_PENDING_EMAIL,
        email_verified=False, business_name=business, business_category=category,
    )
    _issue_verification(conn, user_id, email)
    write_audit(conn, event_type="supplier_registered", success=True,
                user_id=user_id, ip_address=ip, user_agent=ua,
                details={"category": category})
    token, user = login(conn, email, password, ip_address=ip, user_agent=ua)
    return token, user


def verify_email(conn: sqlite3.Connection, raw_token: str, *, ip=None, ua=None) -> dict:
    row = tokens.consume_token(conn, raw_token or "", tokens.PURPOSE_EMAIL_VERIFICATION)
    if row is None:
        raise AuthError("This verification link is invalid or has expired.", status=400)
    user_row = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    if user_row is None or not user_row["is_active"]:
        raise AuthError("This verification link is invalid or has expired.", status=400)
    now = _now_iso()
    cols = _user_cols(conn)
    next_state = _row_get(user_row, "account_state", STATE_ACTIVE)
    kind = _row_get(user_row, "account_kind", KIND_EMPLOYEE)
    if kind == KIND_SUPPLIER and next_state == STATE_PENDING_EMAIL:
        next_state = STATE_PENDING_APPROVAL
    sets = ["updated_at=?"]
    vals: list = [now]
    if "email_verified_at" in cols:
        sets.append("email_verified_at=?")
        vals.append(now)
    if "account_state" in cols:
        sets.append("account_state=?")
        vals.append(next_state)
    vals.append(user_row["id"])
    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    write_audit(conn, event_type="email_verified", success=True,
                user_id=user_row["id"], ip_address=ip, user_agent=ua)
    return build_user_context(conn, conn.execute(
        "SELECT * FROM users WHERE id=?", (user_row["id"],)).fetchone())


def resend_verification(conn: sqlite3.Connection, email: str, *,
                        user=None, ip=None, ua=None) -> dict:
    """Always acknowledges. Does not reveal whether the account exists."""
    target = user
    if target is None:
        row = _get_user_row(conn, normalize_email(email))
        if row is not None and row["is_active"]:
            target = build_user_context(conn, row)
    if target and not is_email_verified(target):
        _issue_verification(conn, target["id"], target["email"])
    write_audit(conn, event_type="verification_resend", success=True,
                user_id=target["id"] if target else None, ip_address=ip, user_agent=ua)
    return {"ok": True, "message": GENERIC_RESEND_ACK}


def request_password_reset(conn: sqlite3.Connection, email: str, *,
                           ip=None, ua=None) -> dict:
    """Always acknowledges. Does not reveal whether the account exists."""
    row = _get_user_row(conn, normalize_email(email))
    if row is not None and row["is_active"]:
        raw = tokens.issue_token(
            conn, row["id"], tokens.PURPOSE_PASSWORD_RESET,
            tokens.default_ttl(tokens.PURPOSE_PASSWORD_RESET),
        )
        mailer.password_reset_email(row["email"], raw, user_id=row["id"])
        write_audit(conn, event_type="password_reset_requested", success=True,
                    user_id=row["id"], ip_address=ip, user_agent=ua)
    else:
        _dummy_password_work()
        write_audit(conn, event_type="password_reset_requested", success=False,
                    ip_address=ip, user_agent=ua, details={"reason": "no_active_user"})
    return {"ok": True, "message": GENERIC_RESET_ACK}


def reset_password(conn: sqlite3.Connection, raw_token: str, new_password: str, *,
                   ip=None, ua=None) -> dict:
    row = tokens.consume_token(conn, raw_token or "", tokens.PURPOSE_PASSWORD_RESET)
    if row is None:
        raise AuthError("This reset link is invalid or has expired.", status=400)
    password = _validate_password(new_password)
    user_row = conn.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
    if user_row is None or not user_row["is_active"]:
        raise AuthError("This reset link is invalid or has expired.", status=400)
    conn.execute(
        "UPDATE users SET password_hash=?, must_change_password=0, failed_login_count=0, "
        "locked_until=NULL, updated_at=? WHERE id=?",
        (passwords.hash_password(password), _now_iso(), user_row["id"]),
    )
    conn.commit()
    sessions.revoke_user_sessions(conn, user_row["id"])
    write_audit(conn, event_type="password_reset", success=True,
                user_id=user_row["id"], ip_address=ip, user_agent=ua)
    return {"ok": True}


def update_self_profile(conn: sqlite3.Connection, user: dict, body: dict) -> dict:
    """Non-sensitive onboarding profile fields. Email and role cannot change."""
    require_not_suspended(user)
    cols = _user_cols(conn)
    updates = {}
    if "name" in body or "display_name" in body:
        name = _clean_name(body.get("name") or body.get("display_name"))
        if not name:
            raise AuthError("Name is required.", status=400)
        updates["display_name"] = name
        first, _, last = name.partition(" ")
        updates["first_name"] = first
        updates["last_name"] = last.strip()
    if "business_name" in body and "business_name" in cols:
        business = _clean_business(body.get("business_name"))
        if not business:
            raise AuthError("Business name is required.", status=400)
        updates["business_name"] = business
    if "phone" in body:
        required = account_kind(user) == KIND_SUPPLIER
        updates["phone"] = _validate_phone(body.get("phone"), required=required)
    if not updates:
        return build_user_context(conn, conn.execute(
            "SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())
    updates["updated_at"] = _now_iso()
    set_sql = ", ".join(f"{k}=?" for k in updates)
    conn.execute(f"UPDATE users SET {set_sql} WHERE id=?", [*updates.values(), user["id"]])
    conn.commit()
    return build_user_context(conn, conn.execute(
        "SELECT * FROM users WHERE id=?", (user["id"],)).fetchone())


def approve_supplier(conn: sqlite3.Connection, actor: dict, target_id: int, *,
                     ip=None, ua=None) -> dict:
    require_permission(actor, "users.update")
    row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    if row is None or row["organization_id"] != actor["organization_id"]:
        raise AuthzError("user not found", status=404)
    kind = _row_get(row, "account_kind", KIND_EMPLOYEE)
    if kind != KIND_SUPPLIER:
        raise AuthError("Only supplier accounts require this approval.", status=400)
    if not _row_get(row, "email_verified_at"):
        raise AuthError("Supplier email must be verified before approval.", status=400)
    conn.execute(
        "UPDATE users SET account_state=?, is_active=1, updated_at=? WHERE id=?",
        (STATE_ACTIVE, _now_iso(), target_id),
    )
    conn.commit()
    write_audit(conn, event_type="supplier_approved", success=True,
                user_id=actor["id"], organization_id=actor["organization_id"],
                resource_type="user", resource_id=target_id, ip_address=ip, user_agent=ua)
    return build_user_context(conn, conn.execute(
        "SELECT * FROM users WHERE id=?", (target_id,)).fetchone())


def suspend_account(conn: sqlite3.Connection, actor: dict, target_id: int, *,
                    ip=None, ua=None) -> dict:
    require_permission(actor, "users.disable")
    row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    if row is None or row["organization_id"] != actor["organization_id"]:
        raise AuthzError("user not found", status=404)
    conn.execute(
        "UPDATE users SET account_state=?, is_active=0, updated_at=? WHERE id=?",
        (STATE_SUSPENDED, _now_iso(), target_id),
    )
    conn.commit()
    sessions.revoke_user_sessions(conn, target_id)
    write_audit(conn, event_type="account_suspended", success=True,
                user_id=actor["id"], resource_type="user", resource_id=target_id,
                ip_address=ip, user_agent=ua)
    return build_user_context(conn, conn.execute(
        "SELECT * FROM users WHERE id=?", (target_id,)).fetchone())


def contractor_dashboard(user: dict) -> dict:
    require_contractor(user)
    require_not_suspended(user)
    verified = is_email_verified(user)
    return {
        "welcome": True,
        "email_verified": verified,
        "account_state": account_state(user),
        "business_name": user.get("business_name"),
        "display_name": user.get("display_name"),
        "verification_message": None if verified else VERIFY_REQUIRED,
        "allowed": [
            "welcome_dashboard",
            "onboarding_profile",
            "public_demonstrations",
            "platform_education",
        ],
        "locked": [
            {"key": "live_rfq", "permission": "contractor.rfq.submit",
             "message": VERIFY_REQUIRED},
            {"key": "protected_intelligence", "permission": "contractor.intelligence.view",
             "message": VERIFY_REQUIRED},
            {"key": "private_quotations", "permission": "contractor.quotes.view",
             "message": VERIFY_REQUIRED},
        ] if not verified else [],
    }


def supplier_dashboard(user: dict) -> dict:
    if account_kind(user) != KIND_SUPPLIER and "supplier" not in (user.get("roles") or []):
        raise AuthzError("permission denied", status=403)
    require_not_suspended(user)
    live = is_email_verified(user) and account_state(user) == STATE_ACTIVE
    return {
        "welcome": True,
        "email_verified": is_email_verified(user),
        "account_state": account_state(user),
        "business_name": user.get("business_name"),
        "business_category": user.get("business_category"),
        "display_name": user.get("display_name"),
        "verification_message": None if live else SUPPLIER_APPROVAL_REQUIRED,
        "allowed": [
            "welcome_dashboard",
            "public_demonstrations",
            "platform_education",
            "onboarding_profile",
        ],
        "locked": [] if live else [
            {"key": "live_rfqs", "permission": "supplier.rfq.access",
             "message": SUPPLIER_APPROVAL_REQUIRED},
            {"key": "quote_submission", "permission": "supplier.quote.submit",
             "message": SUPPLIER_APPROVAL_REQUIRED},
            {"key": "contractor_contacts", "permission": "supplier.contacts.view",
             "message": SUPPLIER_APPROVAL_REQUIRED},
            {"key": "commercial_data", "permission": "supplier.commercial.view",
             "message": SUPPLIER_APPROVAL_REQUIRED},
        ],
    }


def deny_protected_contractor(user: dict, permission: str) -> None:
    require_contractor(user)
    require_not_suspended(user)
    require_verified(user)
    if not has_permission(user, permission):
        raise AuthzError(f"missing permission: {permission}")


def deny_protected_supplier(user: dict, permission: str) -> None:
    require_supplier_live(user)
    if not has_permission(user, permission):
        raise AuthzError(f"missing permission: {permission}")
