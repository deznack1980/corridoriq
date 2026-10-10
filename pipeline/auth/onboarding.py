"""Self-service contractor and supplier accounts (portal database).

Ported from the frictionless-onboarding branch onto production identity
primitives. Nothing here re-implements security logic:

* passwords and accounts: ``service.create_user`` / ``service.login`` (Argon2id,
  lockout, audit);
* email tokens: ``tokens.issue`` / ``tokens.consume`` (hashed, purpose-bound,
  single use with an atomic conditional consume, re-issue revokes older tokens,
  bound to the address the link was sent to);
* delivery: ``mail`` (Postmark or strict SMTP; links carry the token in the URL
  fragment; production never uses the in-memory sink);
* verification state: ``service.email_is_verified`` (valid only while the
  verified address equals the current email);
* rate limits: applied by the route layer with ``throttle.portal``.

Employee accounts are still created only by administrators. This module never
assigns the admin role, and employee CRM/admin routes refuse these accounts
(:func:`require_employee_workspace`).
"""

from __future__ import annotations

import re
import sqlite3

from pipeline.auth import mail, passwords, sessions, tokens
from pipeline.auth.rbac import AuthzError, has_permission, require_permission
from pipeline.auth.seed import DEFAULT_ORG_SLUG
from pipeline.auth.service import (
    AuthError,
    _get_user_row,
    _row_get,
    build_user_context,
    create_user,
    email_is_verified,
    login,
    mark_email_verified,
    normalize_email,
    write_audit,
)

KIND_EMPLOYEE = "employee"
KIND_CONTRACTOR = "contractor"
KIND_SUPPLIER = "supplier"
NETWORK_KINDS = (KIND_CONTRACTOR, KIND_SUPPLIER)

STATE_PENDING_EMAIL = "PENDING_EMAIL_VERIFICATION"
STATE_PENDING_APPROVAL = "PENDING_SUPPLIER_APPROVAL"
STATE_ACTIVE = "ACTIVE"
STATE_SUSPENDED = "SUSPENDED"

GENERIC_REGISTER_ERROR = (
    "Could not create this account. If you already have one, sign in or reset your password."
)
GENERIC_RESEND_ACK = "If this account needs verification, a new link is on its way."
INVALID_VERIFY_LINK = "This verification link is invalid or has expired."
VERIFY_REQUIRED = "Email verification required to activate this feature."
SUPPLIER_APPROVAL_REQUIRED = (
    "Supplier access requires email verification and administrative approval."
)

SUPPLIER_CATEGORIES = ("plumbing", "hvac", "electrical", "general_building", "mechanical", "other")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[0-9+().\- ]{7,25}$")


# ---- account classification ------------------------------------------------

def account_kind(user: dict) -> str:
    return (user or {}).get("account_kind") or KIND_EMPLOYEE


def account_state(user: dict) -> str:
    return (user or {}).get("account_state") or STATE_ACTIVE


def is_network_account(user: dict) -> bool:
    return account_kind(user) in NETWORK_KINDS


def onboarding_verified(user: dict) -> bool:
    """Network accounts need an address-bound verification; employees are not gated."""
    if not is_network_account(user):
        return True
    return bool((user or {}).get("email_verified"))


def require_not_suspended(user: dict) -> None:
    if account_state(user) == STATE_SUSPENDED or not (user or {}).get("is_active", True):
        raise AuthzError("account suspended", status=403)


def require_contractor(user: dict) -> None:
    if account_kind(user) != KIND_CONTRACTOR:
        raise AuthzError("permission denied", status=403)
    require_not_suspended(user)


def require_supplier(user: dict) -> None:
    if account_kind(user) != KIND_SUPPLIER:
        raise AuthzError("permission denied", status=403)
    require_not_suspended(user)


def require_supplier_live(user: dict) -> None:
    require_supplier(user)
    if not onboarding_verified(user) or account_state(user) != STATE_ACTIVE:
        raise AuthzError(SUPPLIER_APPROVAL_REQUIRED, status=403)


def require_employee_workspace(user: dict) -> None:
    """Sales, intelligence and admin APIs stay employee-only (defence in depth)."""
    if is_network_account(user):
        raise AuthzError("permission denied", status=403)
    roles = set((user or {}).get("roles") or [])
    if roles and roles <= set(NETWORK_KINDS):
        raise AuthzError("permission denied", status=403)


def deny_protected_contractor(user: dict, permission: str) -> None:
    require_contractor(user)
    if not onboarding_verified(user):
        raise AuthzError(VERIFY_REQUIRED, status=403)
    if not has_permission(user, permission):
        raise AuthzError(f"missing permission: {permission}")


def deny_protected_supplier(user: dict, permission: str) -> None:
    require_supplier_live(user)
    if not has_permission(user, permission):
        raise AuthzError(f"missing permission: {permission}")


# ---- validation ------------------------------------------------------------------

def _clean(value, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _validate_email(email) -> str:
    norm = normalize_email(email if isinstance(email, str) else "")
    if not norm or len(norm) > 254 or not _EMAIL_RE.fullmatch(norm):
        raise AuthError("Enter a valid email address.", status=400)
    return norm


def _validate_password(password) -> str:
    if not isinstance(password, str) or len(password) < 8 or len(password) > 200:
        raise AuthError("Password must be at least 8 characters.", status=400)
    return password


def _validate_phone(phone, *, required: bool) -> str | None:
    cleaned = _clean(phone, 40) or None
    if not cleaned:
        if required:
            raise AuthError("Business phone is required.", status=400)
        return None
    if not _PHONE_RE.fullmatch(cleaned):
        raise AuthError("Enter a valid business phone.", status=400)
    return cleaned


def default_org_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT id FROM organizations WHERE slug=?", (DEFAULT_ORG_SLUG,)).fetchone()
    if row is None:
        raise AuthError("Platform organization is not available.", status=503)
    return row["id"]


# ---- delivery ----------------------------------------------------------------------

def _deliver(message) -> bool:
    """Send identity mail. Never raises; never claims delivery on failure."""
    try:
        mail.send(message)
        return True
    except Exception:
        return False


def _issue_verification(conn: sqlite3.Connection, user_id: int, email: str, *, ip=None) -> bool:
    raw, expires = tokens.issue(conn, purpose=tokens.PORTAL_VERIFY, ttl=tokens.VERIFY_TTL,
                                user_id=user_id, email=email, ip=ip)
    conn.commit()
    link = mail.token_link("verify-email.html", "verify", raw)
    delivered = _deliver(mail.portal_email_verification(email, link, expires))
    if not delivered:
        tokens.revoke(conn, purpose=tokens.PORTAL_VERIFY, user_id=user_id, email=email)
        conn.commit()
    write_audit(conn, event_type="verification_sent", success=delivered, user_id=user_id,
                ip_address=ip, details={"result": "sent" if delivered else "mail_unavailable"})
    return delivered


# ---- registration ------------------------------------------------------------------

def register(conn: sqlite3.Connection, kind: str, body: dict, *, ip=None, ua=None):
    """Create a contractor or supplier account and sign it in.

    Returns ``(session_token, user_context, verification_sent)``. An existing
    email gets a generic 409 and a no-token "already registered" notice.
    """
    if kind not in NETWORK_KINDS:
        raise AuthError("invalid account type", status=400)
    body = body if isinstance(body, dict) else {}
    name = _clean(body.get("name") or body.get("display_name") or body.get("contact_name"), 120)
    business = _clean(body.get("business_name"), 200)
    email = _validate_email(body.get("email") or body.get("business_email") or "")
    password = _validate_password(body.get("password"))
    if not name:
        raise AuthError("Name is required." if kind == KIND_CONTRACTOR else "Contact name is required.",
                        status=400)
    if not business:
        raise AuthError("Business name is required.", status=400)
    phone, category = None, None
    if kind == KIND_SUPPLIER:
        phone = _validate_phone(body.get("phone") or body.get("business_phone"), required=True)
        category = str(body.get("business_category") or body.get("category") or "").strip().lower()
        if category not in SUPPLIER_CATEGORIES:
            raise AuthError("Select a valid business category.", status=400)

    existing = _get_user_row(conn, email)
    if existing is not None:
        # Equalise work with the create path, notify the real owner, reveal nothing more.
        passwords.verify_password(password, passwords.dummy_password_hash())
        _deliver(mail.portal_already_registered(email))
        write_audit(conn, event_type="register_duplicate", success=False, user_id=existing["id"],
                    ip_address=ip, user_agent=ua, details={"kind": kind})
        raise AuthError(GENERIC_REGISTER_ERROR, status=409)

    first, _, last = name.partition(" ")
    try:
        user_id = create_user(
            conn, organization_id=default_org_id(conn), email=email, password=password,
            first_name=first, last_name=last.strip(), display_name=name, phone=phone,
            role_names=[kind], must_change_password=False,
            account_kind=kind,
            account_state=STATE_ACTIVE if kind == KIND_CONTRACTOR else STATE_PENDING_EMAIL,
            business_name=business, business_category=category,
        )
    except (ValueError, sqlite3.IntegrityError):
        # Lost a race with a concurrent registration of the same address.
        raise AuthError(GENERIC_REGISTER_ERROR, status=409) from None
    write_audit(conn, event_type=f"{kind}_registered", success=True, user_id=user_id,
                ip_address=ip, user_agent=ua, details={"category": category} if category else None)
    sent = _issue_verification(conn, user_id, email, ip=ip)
    token, user = login(conn, email, password, ip_address=ip, user_agent=ua)
    return token, user, sent


def verify_email(conn: sqlite3.Connection, raw_token, *, ip=None, ua=None) -> dict:
    """Consume a verification link. Suppliers move on to administrator approval."""
    user_id = None
    try:
        with tokens.transaction(conn):
            preview = tokens.inspect(conn, raw_token, purpose=tokens.PORTAL_VERIFY)
            if preview is None or preview["user_id"] is None:
                raise tokens.TokenError()
            row = conn.execute("SELECT * FROM users WHERE id=?", (preview["user_id"],)).fetchone()
            if (row is None or not row["is_active"]
                    or _row_get(row, "account_kind") not in NETWORK_KINDS
                    or _row_get(row, "account_state") == STATE_SUSPENDED):
                raise tokens.TokenError()
            current = row["normalized_email"]
            # Bound to the address the link was sent to: a changed email burns it.
            tokens.consume(conn, raw_token, purpose=tokens.PORTAL_VERIFY,
                           user_id=row["id"], email=current)
            mark_email_verified(conn, row["id"], current)
            state = _row_get(row, "account_state")
            if _row_get(row, "account_kind") == KIND_SUPPLIER and state == STATE_PENDING_EMAIL:
                conn.execute("UPDATE users SET account_state=? WHERE id=?",
                             (STATE_PENDING_APPROVAL, row["id"]))
            user_id = row["id"]
            write_audit(conn, event_type="email_verified", success=True, user_id=user_id,
                        ip_address=ip, user_agent=ua, commit=False)
    except tokens.TokenError:
        write_audit(conn, event_type="email_verified", success=False, ip_address=ip,
                    user_agent=ua, details={"reason": "invalid_token"})
        raise AuthError(INVALID_VERIFY_LINK, status=400) from None
    return build_user_context(conn, conn.execute("SELECT * FROM users WHERE id=?",
                                                 (user_id,)).fetchone())


def resend_verification(conn: sqlite3.Connection, email, *, session_user: dict | None = None,
                        ip=None, ua=None) -> dict:
    """Always the same answer; issues a new link only for an unverified network account."""
    row = None
    if session_user:
        row = conn.execute("SELECT * FROM users WHERE id=?", (session_user["id"],)).fetchone()
    elif isinstance(email, str) and email.strip():
        row = _get_user_row(conn, normalize_email(email))
    if (row is not None and row["is_active"]
            and _row_get(row, "account_kind") in NETWORK_KINDS
            and _row_get(row, "account_state") != STATE_SUSPENDED
            and not email_is_verified(row)):
        _issue_verification(conn, row["id"], row["normalized_email"], ip=ip)
    write_audit(conn, event_type="verification_resend", success=True,
                user_id=row["id"] if row is not None else None, ip_address=ip, user_agent=ua)
    return {"ok": True, "message": GENERIC_RESEND_ACK}


# ---- self-service profile ----------------------------------------------------------

def update_self_profile(conn: sqlite3.Connection, user: dict, body: dict) -> dict:
    """Non-sensitive profile fields only. Email, role and state cannot change here."""
    if not is_network_account(user):
        raise AuthzError("permission denied", status=403)
    require_not_suspended(user)
    body = body if isinstance(body, dict) else {}
    updates = {}
    if "name" in body or "display_name" in body:
        name = _clean(body.get("name") or body.get("display_name"), 120)
        if not name:
            raise AuthError("Name is required.", status=400)
        first, _, last = name.partition(" ")
        updates.update(display_name=name, first_name=first, last_name=last.strip())
    if "business_name" in body:
        business = _clean(body.get("business_name"), 200)
        if not business:
            raise AuthError("Business name is required.", status=400)
        updates["business_name"] = business
    if "phone" in body:
        updates["phone"] = _validate_phone(body.get("phone"),
                                           required=account_kind(user) == KIND_SUPPLIER)
    if updates:
        from pipeline.auth.service import _iso, _now
        updates["updated_at"] = _iso(_now())
        conn.execute(f"UPDATE users SET {', '.join(f'{k}=?' for k in updates)} WHERE id=?",
                     [*updates.values(), user["id"]])
        conn.commit()
    return build_user_context(conn, conn.execute("SELECT * FROM users WHERE id=?",
                                                 (user["id"],)).fetchone())


# ---- administrator controls --------------------------------------------------------

def _target(conn, actor: dict, target_id: int):
    row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    if row is None or row["organization_id"] != actor["organization_id"]:
        raise AuthzError("user not found", status=404)
    return row


def approve_supplier(conn: sqlite3.Connection, actor: dict, target_id: int, *,
                     ip=None, ua=None) -> dict:
    """PENDING_SUPPLIER_APPROVAL -> ACTIVE only. Never re-enables suspended accounts."""
    require_permission(actor, "users.update")
    row = _target(conn, actor, target_id)
    if _row_get(row, "account_kind") != KIND_SUPPLIER:
        raise AuthError("Only supplier accounts require this approval.", status=400)
    state = _row_get(row, "account_state")
    if not row["is_active"] or state != STATE_PENDING_APPROVAL:
        raise AuthError("Only suppliers awaiting approval can be approved.", status=409)
    if not email_is_verified(row):
        raise AuthError("Supplier email must be verified before approval.", status=400)
    conn.execute("UPDATE users SET account_state=? WHERE id=? AND account_state=?",
                 (STATE_ACTIVE, target_id, STATE_PENDING_APPROVAL))
    write_audit(conn, event_type="supplier_approved", success=True, user_id=actor["id"],
                organization_id=actor["organization_id"], resource_type="user",
                resource_id=target_id, ip_address=ip, user_agent=ua,
                details={"from": state, "to": STATE_ACTIVE})
    return build_user_context(conn, conn.execute("SELECT * FROM users WHERE id=?",
                                                 (target_id,)).fetchone())


def suspend_account(conn: sqlite3.Connection, actor: dict, target_id: int, *,
                    ip=None, ua=None) -> dict:
    """Suspend a contractor or supplier account and end its sessions immediately."""
    require_permission(actor, "users.disable")
    row = _target(conn, actor, target_id)
    if row["id"] == actor["id"]:
        raise AuthError("You cannot suspend your own account.", status=400)
    if _row_get(row, "account_kind") not in NETWORK_KINDS:
        raise AuthError("Employee accounts are disabled from user management.", status=400)
    state = _row_get(row, "account_state")
    from pipeline.auth.service import _iso, _now
    conn.execute("UPDATE users SET account_state=?, is_active=0, updated_at=? WHERE id=?",
                 (STATE_SUSPENDED, _iso(_now()), target_id))
    sessions.revoke_user_sessions(conn, target_id, commit=False)
    for purpose in (tokens.PORTAL_VERIFY, tokens.PORTAL_RESET):
        tokens.revoke(conn, purpose=purpose, user_id=target_id, email=row["normalized_email"])
    write_audit(conn, event_type="account_suspended", success=True, user_id=actor["id"],
                organization_id=actor["organization_id"], resource_type="user",
                resource_id=target_id, ip_address=ip, user_agent=ua,
                details={"from": state, "to": STATE_SUSPENDED}, commit=False)
    conn.commit()
    return build_user_context(conn, conn.execute("SELECT * FROM users WHERE id=?",
                                                 (target_id,)).fetchone())


# ---- welcome workspaces (no live data) ---------------------------------------------

def contractor_dashboard(user: dict) -> dict:
    require_contractor(user)
    verified = onboarding_verified(user)
    return {
        "welcome": True,
        "email_verified": verified,
        "account_state": account_state(user),
        "business_name": user.get("business_name"),
        "display_name": user.get("display_name"),
        "verification_message": None if verified else VERIFY_REQUIRED,
        "value": ("Build a material list, send it only to the supplier you choose, "
                  "and compare their response in one place."),
        "allowed": ["welcome_dashboard", "onboarding_profile", "public_demonstrations",
                    "platform_education"],
        "available_now": [
            {"title": "Welcome workspace", "detail": "Signed in. Your contractor account is ready."},
            {"title": "Business profile", "detail": "Name and company — no live request is sent."},
            {"title": "Workflow demonstration",
             "detail": "See how a bill of materials becomes a supplier response."},
        ],
        "workflow_demo": {
            "label": "Demonstration — not a live request",
            "project": "Sample medical-office tenant improvement · Phoenix, AZ",
            "bom": [
                {"line": 1, "description": "PEX-A tubing 3/4 in coil", "qty": "4", "unit": "coil"},
                {"line": 2, "description": "Lead-free ball valve 3/4 in", "qty": "18", "unit": "each"},
                {"line": 3, "description": "Commercial electric water heater 50 gal", "qty": "2",
                 "unit": "each"},
            ],
            "suppliers": [
                {"name": "Sample Supply House A", "status": "Quoted", "availability": "In stock",
                 "lead": "2 days"},
                {"name": "Sample Supply House B", "status": "Quoted", "availability": "Partial",
                 "lead": "5 days"},
            ],
            "note": ("These lines and responses are synthetic. Live RFQs and private quotations "
                     "stay locked until you verify your email. No order is placed here."),
        },
        "locked": [] if verified else [
            {"key": "live_rfq", "permission": "contractor.rfq.submit", "message": VERIFY_REQUIRED},
            {"key": "protected_intelligence", "permission": "contractor.intelligence.view",
             "message": VERIFY_REQUIRED},
            {"key": "private_quotations", "permission": "contractor.quotes.view",
             "message": VERIFY_REQUIRED},
        ],
    }


def supplier_dashboard(user: dict) -> dict:
    require_supplier(user)
    verified = onboarding_verified(user)
    live = verified and account_state(user) == STATE_ACTIVE
    return {
        "welcome": True,
        "email_verified": verified,
        "account_state": account_state(user),
        "business_name": user.get("business_name"),
        "business_category": user.get("business_category"),
        "display_name": user.get("display_name"),
        "verification_message": None if live else SUPPLIER_APPROVAL_REQUIRED,
        "allowed": ["welcome_dashboard", "public_demonstrations", "platform_education",
                    "onboarding_profile"],
        "available_now": [
            {"title": "Supplier welcome workspace",
             "detail": "Signed in. Live contractor data is not shown yet."},
            {"title": "Platform demonstration",
             "detail": "See how a contractor request would reach only your branch."},
        ],
        "onboarding_path": [
            {"state": STATE_PENDING_EMAIL, "detail": "Confirm the business email."},
            {"state": STATE_PENDING_APPROVAL,
             "detail": "An administrator reviews the account. Approval is never automatic."},
            {"state": STATE_ACTIVE, "detail": "Live RFQs, quoting, and contractor contacts unlock."},
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
