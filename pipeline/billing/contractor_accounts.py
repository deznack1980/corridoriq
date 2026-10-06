"""Contractor account provisioning and the contractor signup data contract.

Uses the existing identity model only: a contractor account is an
`organizations` row with account_type='contractor', one owner in `users` with
the `contractor_owner` role, and a `contractor_profiles` row. No second
identity system.

Provisioning is a CorridorIQ-operator action: only an `admin.system` user of
CorridorIQ's own organization may create contractor accounts. The browser can
never choose the account type, role, plan or permissions; they are fixed here.

The owner is invited by a purpose-bound email token (development sink only).
No temporary password is generated or returned. Invitation acceptance sets
the password and marks the invited email verified. `validate_contractor_signup()`
is the data contract for the fields an operator supplies.
"""

from __future__ import annotations

import re
import secrets
import sqlite3

from pipeline.auth.rbac import AuthzError, has_permission
from pipeline.auth.seed import DEFAULT_ORG_SLUG
from pipeline.auth.service import AuthError, create_user, normalize_email, write_audit
from pipeline.billing import store
from pipeline.billing.entitlements import entitlements_for, request_priority
from pipeline.billing.plans import CONTRACTOR

CONTRACTOR_ROLE = "contractor_owner"

# ---- signup data contract ---------------------------------------------------
# required: contact_name, email, company_name, and business_zip OR business_address
# optional: roc_license (Arizona ROC; never required), phone
_EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+$")
_ZIP = re.compile(r"^\d{5}(?:-\d{4})?$")
_ROC = re.compile(r"^[A-Z0-9][A-Z0-9-]{2,19}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
SIGNUP_FIELDS = ("contact_name", "email", "company_name", "business_zip", "business_address", "roc_license", "phone")


class ContractorAccountError(ValueError):
    """Validation failure; `errors` maps field -> message (safe to show)."""

    def __init__(self, errors: dict):
        super().__init__("; ".join(f"{k}: {v}" for k, v in errors.items()))
        self.errors = errors


def _text(data, key, *, max_len, min_len=0):
    value = data.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(key)
    value = " ".join(value.split())
    if _CONTROL.search(value):
        raise TypeError(key)
    if value and not (min_len <= len(value) <= max_len):
        raise ValueError(key)
    return value


def validate_contractor_signup(data) -> dict:
    """Normalize and validate contractor signup fields. Unknown fields are ignored;
    account type, role and plan are never taken from input."""
    if not isinstance(data, dict):
        raise ContractorAccountError({"_": "expected an object"})
    errors, out = {}, {}
    limits = {"contact_name": (2, 120), "company_name": (2, 160), "business_address": (5, 200),
              "email": (3, 254), "business_zip": (5, 10), "roc_license": (3, 20), "phone": (7, 25)}
    for key, (lo, hi) in limits.items():
        try:
            out[key] = _text(data, key, min_len=lo, max_len=hi)
        except (TypeError, ValueError):
            errors[key] = f"must be text of {lo}-{hi} characters"
    if not out.get("contact_name") and "contact_name" not in errors:
        errors["contact_name"] = "is required"
    if not out.get("company_name") and "company_name" not in errors:
        errors["company_name"] = "is required"
    email = normalize_email(out.get("email", ""))
    if "email" not in errors:
        if not email:
            errors["email"] = "is required"
        elif not _EMAIL.fullmatch(email):
            errors["email"] = "is not a valid email address"
    out["email"] = email
    if out.get("business_zip") and "business_zip" not in errors and not _ZIP.fullmatch(out["business_zip"]):
        errors["business_zip"] = "must be a 5-digit ZIP or ZIP+4"
    if not out.get("business_zip") and not out.get("business_address") \
            and "business_zip" not in errors and "business_address" not in errors:
        errors["business_zip"] = "business ZIP or business address is required"
    if out.get("roc_license") and "roc_license" not in errors:
        roc = out["roc_license"].upper().replace(" ", "")
        if not _ROC.fullmatch(roc):
            errors["roc_license"] = "must be letters, digits or hyphens (optional field)"
        out["roc_license"] = roc
    if out.get("phone") and "phone" not in errors:
        digits = re.sub(r"\D", "", out["phone"])
        if not 10 <= len(digits) <= 15:
            errors["phone"] = "must contain 10-15 digits (optional field)"
        out["phone"] = digits
    if errors:
        raise ContractorAccountError(errors)
    return {k: (out.get(k) or None) for k in SIGNUP_FIELDS}


# ---- provisioning -----------------------------------------------------------
def _require_operator(conn, user):
    """CorridorIQ operators only: admin.system AND a member of CorridorIQ's own org."""
    if not has_permission(user, "admin.system"):
        raise AuthzError("Only CorridorIQ administrators can manage contractor accounts.")
    org = conn.execute("SELECT slug FROM organizations WHERE id=?", (user["organization_id"],)).fetchone()
    if org is None or org["slug"] != DEFAULT_ORG_SLUG:
        raise AuthzError("Only CorridorIQ administrators can manage contractor accounts.")


def _slug(name: str, conn) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "contractor"
    base = f"contractor-{base}"
    slug, n = base, 2
    while conn.execute("SELECT 1 FROM organizations WHERE slug=?", (slug,)).fetchone():
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_contractor_account(conn: sqlite3.Connection, operator: dict, data, *, ip=None, ua=None) -> dict:
    _require_operator(conn, operator)
    fields = validate_contractor_signup(data)
    if conn.execute("SELECT 1 FROM users WHERE normalized_email=?", (fields["email"],)).fetchone():
        raise ContractorAccountError({"email": "an account with this email already exists"})
    now = store.now_iso()
    org_id = conn.execute(
        "INSERT INTO organizations (name, slug, is_active, account_type, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?, ?)", (fields["company_name"], _slug(fields["company_name"], conn), CONTRACTOR, now, now)
    ).lastrowid
    conn.execute("INSERT INTO contractor_profiles (organization_id, business_zip, business_address, roc_license, "
                 "created_by, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                 (org_id, fields["business_zip"], fields["business_address"], fields["roc_license"],
                  operator["id"], now, now))
    conn.commit()
    first, _, last = fields["contact_name"].partition(" ")
    placeholder = secrets.token_urlsafe(32)
    try:
        owner_id = create_user(conn, organization_id=org_id, email=fields["email"], password=placeholder,
                               first_name=first, last_name=last, display_name=fields["contact_name"],
                               phone=fields["phone"], role_names=[CONTRACTOR_ROLE], must_change_password=True,
                               created_by=operator["id"])
        _issue_owner_invitation(conn, owner_id, fields["email"], fields["company_name"],
                                org_id, operator_id=operator["id"], ip=ip, ua=ua)
    except Exception:
        conn.execute("DELETE FROM contractor_profiles WHERE organization_id=?", (org_id,))
        conn.execute("DELETE FROM users WHERE organization_id=?", (org_id,))
        conn.execute("DELETE FROM organizations WHERE id=?", (org_id,))
        conn.commit()
        raise
    write_audit(conn, event_type="contractor_account_created", success=True, user_id=operator["id"],
                organization_id=org_id, resource_type="organization", resource_id=org_id,
                action="create_contractor_account", ip_address=ip, user_agent=ua,
                details={"owner_user_id": owner_id, "roc_provided": bool(fields["roc_license"]),
                         "invitation": True})
    return {
        "organization": {"id": org_id, "name": fields["company_name"], "account_type": CONTRACTOR},
        "owner": {"id": owner_id, "email": fields["email"], "display_name": fields["contact_name"],
                  "role": CONTRACTOR_ROLE, "must_change_password": True, "invitation_pending": True},
        "invitation_sent": True,
    }


def _issue_owner_invitation(conn, owner_id, email, company_name, org_id, *, operator_id=None, ip=None, ua=None):
    from pipeline.auth import mail, tokens

    raw, expires = tokens.issue(conn, purpose=tokens.PORTAL_INVITE, ttl=tokens.INVITE_TTL,
                                user_id=owner_id, email=email, context={"organization_id": org_id}, ip=ip)
    conn.commit()
    mail.send(mail.portal_owner_invitation(email, mail.token_link("login.html", "accept-invite", raw),
                                           expires, company_name))
    write_audit(conn, event_type="contractor_owner_invitation_issued", success=True,
                user_id=operator_id, organization_id=org_id, resource_type="user",
                resource_id=owner_id, ip_address=ip, user_agent=ua, details={"email": email})


def accept_owner_invitation(conn: sqlite3.Connection, raw_token, new_password, *, ip=None, ua=None) -> dict:
    """Claim a contractor-owner invitation. Does not open a session."""
    from pipeline.auth import passwords, tokens
    from pipeline.auth.service import mark_email_verified

    if len(new_password or "") < 8 or len(new_password or "") > 200:
        raise AuthError("New password must be at least 8 characters.", status=400)
    try:
        with tokens.transaction(conn):
            preview = tokens.inspect(conn, raw_token, purpose=tokens.PORTAL_INVITE)
            if preview is None:
                raise tokens.TokenError()
            user = conn.execute("SELECT * FROM users WHERE id=?", (preview["user_id"],)).fetchone()
            if user is None or not user["is_active"]:
                raise tokens.TokenError()
            ctx = tokens.context_of(preview)
            if ctx.get("organization_id") not in (None, user["organization_id"]):
                raise tokens.TokenError()
            org = conn.execute("SELECT * FROM organizations WHERE id=?", (user["organization_id"],)).fetchone()
            if org is None or not org["is_active"] or org["account_type"] != CONTRACTOR:
                raise tokens.TokenError()
            tokens.consume(conn, raw_token, purpose=tokens.PORTAL_INVITE,
                           user_id=user["id"], email=user["normalized_email"])
            conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=0, failed_login_count=0, "
                "locked_until=NULL, updated_at=? WHERE id=?",
                (passwords.hash_password(new_password), store.now_iso(), user["id"]),
            )
            mark_email_verified(conn, user["id"], user["normalized_email"])
            tokens.revoke(conn, purpose=tokens.PORTAL_INVITE, user_id=user["id"], email=user["normalized_email"])
            write_audit(conn, event_type="contractor_owner_invitation_accepted", success=True,
                        user_id=user["id"], organization_id=user["organization_id"],
                        ip_address=ip, user_agent=ua, details={"email": user["normalized_email"]},
                        commit=False)
    except tokens.TokenError as exc:
        write_audit(conn, event_type="contractor_owner_invitation_accepted", success=False,
                    ip_address=ip, user_agent=ua, details={"reason": "invalid_token"})
        raise AuthError("invalid or expired token", status=400) from exc
    return {"ok": True}


def list_contractor_accounts(conn: sqlite3.Connection, operator: dict, config=None) -> list[dict]:
    _require_operator(conn, operator)
    rows = conn.execute(
        "SELECT o.id, o.name, o.is_active, o.created_at, p.business_zip, p.roc_license, b.billing_state "
        "FROM organizations o LEFT JOIN contractor_profiles p ON p.organization_id=o.id "
        "LEFT JOIN billing_accounts b ON b.organization_id=o.id WHERE o.account_type=? ORDER BY o.id DESC",
        (CONTRACTOR,)).fetchall()
    out = []
    for r in rows:
        owners = [dict(u) for u in conn.execute(
            "SELECT u.id, u.email, u.display_name, u.is_active, u.must_change_password FROM users u "
            "JOIN user_roles ur ON ur.user_id=u.id JOIN roles ro ON ro.id=ur.role_id "
            "WHERE u.organization_id=? AND ro.name=?", (r["id"], CONTRACTOR_ROLE))]
        out.append({"organization_id": r["id"], "name": r["name"], "is_active": bool(r["is_active"]),
                    "created_at": r["created_at"], "business_zip": r["business_zip"],
                    "roc_license": r["roc_license"], "billing_state": r["billing_state"] or "none",
                    "entitlements": sorted(entitlements_for(conn, r["id"], config)),
                    "request_priority": request_priority(conn, r["id"], config), "owners": owners})
    return out
