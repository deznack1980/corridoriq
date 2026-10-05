"""Pilot accounts: contractor signup, sign-in, the current principal, and
supplier / referral setup.

Authentication is the portal's own code (``pipeline.auth.service``) running
against the pilot platform database: the same hashing, lockout, sessions and
audit log. Each pilot user has exactly one membership — a contractor tenant
(owner or member) or a supplier tenant (inbox) — and every request is resolved
to that tenant through its registry, which refuses unknown or inactive tenants.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from pipeline.auth import service as auth
from pipeline.auth import sessions
from pipeline.pilot import analytics
from pipeline.pilot.platform import (
    ROLE_CONTRACTOR_MEMBER,
    ROLE_CONTRACTOR_OWNER,
    ROLE_SUPPLIER_INBOX,
    Platform,
)
from pipeline.tenancy.model import TenantError, validate_display_name, validate_tenant_id

LANGUAGES = ("en", "es")
REFERRAL_CODE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{2,38})[a-z0-9]$")
_PHONE_RE = re.compile(r"^[0-9+().\- ]{7,25}$")


class PilotError(Exception):
    """User-facing pilot failure. ``code`` is a stable key the UI translates."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Principal:
    user: dict
    kind: str            # "contractor" | "supplier"
    tenant_id: str
    role: str
    ctx: object          # ContractorContext | supplier TenantContext
    language: str

    @property
    def user_id(self) -> int:
        return self.user["id"]


# ---- referral codes ----------------------------------------------------------

def resolve_referral(conn, platform: Platform, code) -> dict | None:
    """Public view of a referral code: the supplier's display name only."""
    if not isinstance(code, str) or not REFERRAL_CODE_RE.fullmatch(code):
        return None
    row = conn.execute("SELECT * FROM referral_codes WHERE code=? AND active=1", (code,)).fetchone()
    if row is None:
        return None
    try:
        supplier = platform.suppliers.get(row["supplier_tenant_id"])
    except TenantError:
        return None
    if not supplier.active:
        return None
    return {"code": row["code"], "supplier_name": supplier.display_name,
            "_supplier_tenant_id": supplier.tenant_id}


def public_referral(info: dict) -> dict:
    return {"code": info["code"], "supplier_name": info["supplier_name"]}


# ---- setup (owner CLI / tests) ---------------------------------------------------

def create_supplier(platform: Platform, tenant_id: str, display_name: str):
    return platform.suppliers.create(tenant_id, display_name)


def create_referral_code(conn, platform: Platform, supplier_tenant_id: str, prefix: str, label=None) -> str:
    supplier = platform.suppliers.get(supplier_tenant_id)
    prefix = re.sub(r"[^a-z0-9-]", "", (prefix or "").lower()).strip("-")[:24] or "join"
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    for _ in range(10):
        code = f"{prefix}-{''.join(secrets.choice(alphabet) for _ in range(6))}"
        if code == supplier.tenant_id or not REFERRAL_CODE_RE.fullmatch(code):
            continue
        try:
            conn.execute("INSERT INTO referral_codes (code, supplier_tenant_id, label, active, created_at) "
                         "VALUES (?,?,?,1,?)", (code, supplier.tenant_id, label, _now()))
            conn.commit()
            return code
        except sqlite3.IntegrityError:
            continue
    raise RuntimeError("could not allocate a referral code")


def add_supplier_user(conn, platform: Platform, supplier_tenant_id: str, email: str, password: str,
                      display_name: str = "") -> int:
    supplier = platform.suppliers.get(supplier_tenant_id)
    org_id = _pilot_org_id(conn)
    user_id = auth.create_user(conn, organization_id=org_id, email=email, password=password,
                               display_name=display_name or None, role_names=[ROLE_SUPPLIER_INBOX],
                               must_change_password=False)
    conn.execute("INSERT INTO memberships (user_id, tenant_kind, tenant_id, role, created_at) "
                 "VALUES (?, 'supplier', ?, ?, ?)", (user_id, supplier.tenant_id, ROLE_SUPPLIER_INBOX, _now()))
    conn.commit()
    return user_id


def _pilot_org_id(conn) -> int:
    return conn.execute("SELECT id FROM organizations WHERE slug='pilot-platform'").fetchone()["id"]


# ---- contractor signup -------------------------------------------------------------

def signup_contractor(conn, platform: Platform, body: dict, *, ip=None, ua=None):
    """Create a contractor tenant, its owner account, and (with a valid referral
    code) a connection to the referring supplier. Returns (token, principal)."""
    email = auth.normalize_email(body.get("email", ""))
    password = body.get("password") or ""
    name = " ".join(str(body.get("name") or "").split())[:120]
    business = body.get("business_name")
    phone = " ".join(str(body.get("phone") or "").split()) or None
    language = body.get("language") if body.get("language") in LANGUAGES else "en"

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 254:
        raise PilotError("invalid_email")
    if len(password) < 8 or len(password) > 200:
        raise PilotError("weak_password")
    if not name:
        raise PilotError("name_required")
    try:
        business = validate_display_name(business)
    except TenantError:
        raise PilotError("business_required")
    if phone and not _PHONE_RE.fullmatch(phone):
        raise PilotError("invalid_phone")
    if conn.execute("SELECT 1 FROM users WHERE normalized_email=?", (email,)).fetchone():
        raise PilotError("email_exists", status=409)

    referral = resolve_referral(conn, platform, body.get("referral_code")) if body.get("referral_code") else None
    utm = analytics.clean_utm(body.get("utm"))
    channel = analytics.derive_channel(utm, referral["code"] if referral else None)
    source = "supplier_referral" if referral else ("campaign" if utm else "direct")

    tenant = platform.contractors.create(business)
    user_id = None
    try:
        user_id = auth.create_user(conn, organization_id=_pilot_org_id(conn), email=email, password=password,
                                   display_name=name, phone=phone, role_names=[ROLE_CONTRACTOR_OWNER],
                                   must_change_password=False)
        now = _now()
        conn.execute("INSERT INTO memberships (user_id, tenant_kind, tenant_id, role, created_at) "
                     "VALUES (?, 'contractor', ?, ?, ?)", (user_id, tenant.tenant_id, ROLE_CONTRACTOR_OWNER, now))
        conn.execute("INSERT INTO user_prefs (user_id, preferred_language, updated_at) VALUES (?,?,?)",
                     (user_id, language, now))
        conn.execute(
            "INSERT INTO contractor_profiles (tenant_id, business_name, owner_user_id, phone, preferred_language, "
            "created_at, acquisition_source, acquisition_channel, referral_code, referring_supplier_tenant_id, "
            "utm_source, utm_medium, utm_campaign, utm_content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tenant.tenant_id, business, user_id, phone, language, now, source, channel,
             referral["code"] if referral else None, referral["_supplier_tenant_id"] if referral else None,
             utm.get("utm_source"), utm.get("utm_medium"), utm.get("utm_campaign"), utm.get("utm_content")))
        if referral:
            _connect_supplier(conn, tenant.tenant_id, referral, source="referral")
        profile = contractor_profile(conn, tenant.tenant_id)
        analytics.record_for_contractor(conn, "signup_completed", profile, user_id=user_id,
                                        visit_id=body.get("visit_id"), page="contractor-account", commit=False)
        conn.commit()
        auth.write_audit(conn, event_type="pilot_signup", success=True, user_id=user_id,
                         resource_type="contractor_tenant", resource_id=tenant.tenant_id,
                         ip_address=ip, user_agent=ua, details={"channel": channel})
    except Exception:
        conn.rollback()
        platform.contractors.set_active(tenant.tenant_id, False)
        if user_id is not None:
            conn.execute("UPDATE users SET is_active=0 WHERE id=?", (user_id,))
            conn.commit()
        raise
    token, _ = auth.login(conn, email, password, ip_address=ip, user_agent=ua)
    return token, principal_for_token(conn, platform, token)


def _connect_supplier(conn, contractor_tenant_id, referral, *, source) -> str:
    row = conn.execute("SELECT connection_id FROM supplier_connections WHERE contractor_tenant_id=? "
                       "AND supplier_tenant_id=?", (contractor_tenant_id, referral["_supplier_tenant_id"])).fetchone()
    if row:
        return row["connection_id"]
    cid = "k-" + secrets.token_hex(8)
    conn.execute("INSERT INTO supplier_connections (connection_id, contractor_tenant_id, supplier_tenant_id, "
                 "source, referral_code, created_at) VALUES (?,?,?,?,?,?)",
                 (cid, contractor_tenant_id, referral["_supplier_tenant_id"], source, referral["code"], _now()))
    return cid


def connect_by_referral(conn, platform: Platform, principal: Principal, code) -> dict:
    require_contractor(principal)
    referral = resolve_referral(conn, platform, code)
    if referral is None:
        raise PilotError("referral_not_found", status=404)
    _connect_supplier(conn, principal.tenant_id, referral, source="referral_after_signup")
    conn.commit()
    return {"connections": list_connections(conn, platform, principal)}


# ---- sign-in / principal -------------------------------------------------------------

def sign_in(conn, platform: Platform, email, password, *, visit_id=None, ip=None, ua=None):
    norm = auth.normalize_email(email or "")
    before = conn.execute("SELECT last_login_at FROM users WHERE normalized_email=?", (norm,)).fetchone()
    token, _ = auth.login(conn, email or "", password or "", ip_address=ip, user_agent=ua)
    principal = principal_for_token(conn, platform, token)
    if principal is None:
        sessions.revoke_session(conn, token)
        raise auth.AuthError(auth.GENERIC_LOGIN_ERROR)
    if principal.kind == "contractor":
        profile = contractor_profile(conn, principal.tenant_id)
        analytics.record_for_contractor(conn, "login", profile, user_id=principal.user_id, visit_id=visit_id)
        if before is not None and before["last_login_at"]:
            analytics.record_for_contractor(conn, "repeat_login", profile, user_id=principal.user_id,
                                            visit_id=visit_id)
    return token, principal


def principal_for_token(conn, platform: Platform, token) -> Principal | None:
    user = auth.get_current_user(conn, token)
    if user is None:
        return None
    m = conn.execute("SELECT * FROM memberships WHERE user_id=?", (user["id"],)).fetchone()
    if m is None:
        return None
    try:
        if m["tenant_kind"] == "contractor":
            ctx = platform.contractors.context(m["tenant_id"])
        else:
            ctx = platform.suppliers.context(m["tenant_id"])
    except TenantError:
        return None
    pref = conn.execute("SELECT preferred_language FROM user_prefs WHERE user_id=?", (user["id"],)).fetchone()
    return Principal(user=user, kind=m["tenant_kind"], tenant_id=m["tenant_id"], role=m["role"], ctx=ctx,
                     language=pref["preferred_language"] if pref else "en")


def require_contractor(principal: Principal) -> Principal:
    if principal is None or principal.kind != "contractor" or principal.role not in (
            ROLE_CONTRACTOR_OWNER, ROLE_CONTRACTOR_MEMBER):
        raise PilotError("forbidden", status=403)
    return principal


def require_supplier(principal: Principal) -> Principal:
    if principal is None or principal.kind != "supplier" or principal.role != ROLE_SUPPLIER_INBOX:
        raise PilotError("forbidden", status=403)
    return principal


def set_language(conn, principal: Principal, language) -> str:
    if language not in LANGUAGES:
        raise PilotError("invalid_language")
    conn.execute("INSERT INTO user_prefs (user_id, preferred_language, updated_at) VALUES (?,?,?) "
                 "ON CONFLICT(user_id) DO UPDATE SET preferred_language=excluded.preferred_language, "
                 "updated_at=excluded.updated_at", (principal.user_id, language, _now()))
    conn.commit()
    principal.language = language
    return language


# ---- contractor profile / connections --------------------------------------------------

def contractor_profile(conn, tenant_id) -> sqlite3.Row:
    return conn.execute("SELECT * FROM contractor_profiles WHERE tenant_id=?", (validate_tenant_id(tenant_id),)).fetchone()


def list_connections(conn, platform: Platform, principal: Principal) -> list[dict]:
    require_contractor(principal)
    out = []
    for r in conn.execute("SELECT * FROM supplier_connections WHERE contractor_tenant_id=? ORDER BY created_at",
                          (principal.tenant_id,)):
        try:
            supplier = platform.suppliers.get(r["supplier_tenant_id"])
        except TenantError:
            continue
        if supplier.active:
            out.append({"connection_id": r["connection_id"], "supplier_name": supplier.display_name,
                        "since": r["created_at"]})
    return out


def connection_supplier(conn, platform: Platform, principal: Principal, connection_id) -> tuple[str, str]:
    """(supplier tenant ID, display name) for one of this contractor's connections."""
    require_contractor(principal)
    row = conn.execute("SELECT supplier_tenant_id FROM supplier_connections WHERE connection_id=? "
                       "AND contractor_tenant_id=?", (str(connection_id or ""), principal.tenant_id)).fetchone()
    if row is None:
        raise PilotError("connection_not_found", status=404)
    supplier = platform.suppliers.context(row["supplier_tenant_id"]).tenant
    return supplier.tenant_id, supplier.display_name


def me(conn, platform: Platform, principal: Principal) -> dict:
    out = {"user": {"display_name": principal.user["display_name"], "email": principal.user["email"]},
           "account_type": principal.kind, "role": principal.role, "language": principal.language}
    if principal.kind == "contractor":
        p = contractor_profile(conn, principal.tenant_id)
        if p["onboarded_at"] is None:
            conn.execute("UPDATE contractor_profiles SET onboarded_at=? WHERE tenant_id=?", (_now(), p["tenant_id"]))
            analytics.record_for_contractor(conn, "contractor_onboarding_completed", p, user_id=principal.user_id,
                                            page="contractor-home", commit=False)
            conn.commit()
        out["contractor"] = {
            "business_name": p["business_name"], "phone": p["phone"],
            "email_verification": "pending",
            "is_owner": principal.role == ROLE_CONTRACTOR_OWNER,
        }
        out["connections"] = list_connections(conn, platform, principal)
    else:
        out["supplier"] = {"name": principal.ctx.tenant.display_name}
    return out


def update_profile(conn, platform: Platform, principal: Principal, body: dict) -> dict:
    require_contractor(principal)
    if principal.role != ROLE_CONTRACTOR_OWNER:
        raise PilotError("forbidden", status=403)
    try:
        business = validate_display_name(body.get("business_name"))
    except TenantError:
        raise PilotError("business_required")
    phone = " ".join(str(body.get("phone") or "").split()) or None
    if phone and not _PHONE_RE.fullmatch(phone):
        raise PilotError("invalid_phone")
    conn.execute("UPDATE contractor_profiles SET business_name=?, phone=? WHERE tenant_id=?",
                 (business, phone, principal.tenant_id))
    conn.commit()
    platform.contractors.rename(principal.tenant_id, business)
    return me(conn, platform, principal)
