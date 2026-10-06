"""HTTP routes for the contractor pilot (``/api/pilot/...``).

The portal server hands these requests over before it opens the intelligence
database, so pilot routes never touch it. Pilot sessions use their own cookie.
Every write must be ``application/json`` (blocks cross-site form posts).
Unknown IDs and other tenants' IDs both answer 404.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from urllib.parse import unquote

from pipeline.auth import throttle
from pipeline.auth.service import (
    AuthError, logout, request_password_reset, complete_password_reset, normalize_email,
)
from pipeline.auth import tokens as auth_tokens
from pipeline.pilot import accounts, analytics, materials
from pipeline.pilot.accounts import PilotError
from pipeline.pilot.platform import PILOT_COOKIE, Platform, PilotNotConfigured, configured_root
from pipeline.tenancy.model import TenantError

_REQ = re.compile(r"^/api/pilot/contractor/requests/(r-[0-9a-f]{16})(/send)?$")
_SHARE = re.compile(r"^/api/pilot/supplier/requests/(s-[0-9a-f]{16})(/acknowledge)?$")
_REFERRAL = re.compile(r"^/api/pilot/referral/([^/]{1,64})$")

_PUBLIC_WRITES = {
    "/api/pilot/auth/signup", "/api/pilot/auth/complete-signup", "/api/pilot/auth/inspect",
    "/api/pilot/auth/login", "/api/pilot/auth/forgot", "/api/pilot/auth/reset", "/api/pilot/events",
}

# Events keep the original coarse per-IP limiter. Identity flows use throttle.pilot.
_WINDOW_S = 600
_LIMITS = {"/api/pilot/events": 300}
_hits: dict = {}
_hits_lock = threading.Lock()


def _throttled(path: str, ip) -> bool:
    limit = _LIMITS.get(path)
    if not limit:
        return False
    now = time.monotonic()
    with _hits_lock:
        q = _hits.setdefault((path, ip), deque())
        while q and now - q[0] > _WINDOW_S:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
    return False


def _platform() -> Platform:
    return Platform(configured_root())


def handle(handler, method: str, path: str, query: dict, body: dict | None) -> None:
    """Entry point from ApiHandler. ``handler`` provides _json/_token/_client."""
    try:
        platform = _platform()
    except PilotNotConfigured:
        return handler._json(503, {"error": "pilot_not_enabled"})
    conn = platform.connect()
    try:
        _route(handler, platform, conn, method, path, query, body or {})
    except PilotError as exc:
        handler._json(exc.status, {"error": exc.code})
    except AuthError as exc:
        handler._json(exc.status, {"error": "invalid_login"})
    except TenantError:
        handler._json(403, {"error": "forbidden"})
    except Exception:  # pragma: no cover - never leak internals
        handler._json(500, {"error": "server_error"})
    finally:
        conn.close()


def _cookie(handler, token: str | None) -> str:
    from pipeline.config import settings
    if token is None:
        return f"{PILOT_COOKIE}=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax"
    parts = [f"{PILOT_COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
             f"Max-Age={settings.SESSION_TTL_HOURS * 3600}"]
    if settings.AUTH_PRODUCTION:
        parts.append("Secure")
    return "; ".join(parts)


def _route(h, platform: Platform, conn, method, path, query, body):
    ip, ua = h._client()
    token = h._pilot_token()

    if method in ("POST", "PATCH") and not h._is_json():
        raise PilotError("json_required", status=415)

    # ---- public -------------------------------------------------------------
    if method == "GET":
        m = _REFERRAL.match(path)
        if m:
            info = accounts.resolve_referral(conn, platform, unquote(m.group(1)))
            if info is None:
                raise PilotError("referral_not_found", status=404)
            return h._json(200, accounts.public_referral(info))
    if method == "POST" and path in _PUBLIC_WRITES:
        if path == "/api/pilot/events":
            if _throttled(path, ip):
                raise PilotError("too_many_requests", status=429)
            return _client_event(h, platform, conn, token, body)
        if path == "/api/pilot/auth/signup":
            email = (body.get("email") or "").strip().lower()
            if throttle.pilot.hit("signup_ip", str(ip or "unknown")):
                raise PilotError("too_many_requests", status=429)
            if email and throttle.pilot.hit("signup_email", email):
                return h._json(202, {"status": "check_email"})
            return h._json(202, accounts.request_contractor_signup(conn, platform, body, ip=ip, ua=ua))
        if path == "/api/pilot/auth/complete-signup":
            if throttle.pilot.hit("signup_ip", str(ip or "unknown")):
                raise PilotError("too_many_requests", status=429)
            new_token, principal = accounts.complete_contractor_signup(conn, platform, body, ip=ip, ua=ua)
            return h._json(201, accounts.me(conn, platform, principal), raw_cookie=_cookie(h, new_token))
        if path == "/api/pilot/auth/inspect":
            purpose = body.get("purpose") or auth_tokens.PILOT_SIGNUP
            return h._json(200, accounts.inspect_auth_token(conn, body.get("token"), purpose=purpose))
        if path == "/api/pilot/auth/login":
            if throttle.pilot.hit("login_ip", str(ip or "unknown")):
                raise PilotError("too_many_requests", status=429)
            new_token, principal = accounts.sign_in(conn, platform, body.get("email"), body.get("password"),
                                                    visit_id=body.get("visit_id"), ip=ip, ua=ua)
            return h._json(200, accounts.me(conn, platform, principal), raw_cookie=_cookie(h, new_token))
        if path == "/api/pilot/auth/forgot":
            email = (body.get("email") or "").strip().lower()
            if throttle.pilot.hit("forgot_ip", str(ip or "unknown")):
                raise PilotError("too_many_requests", status=429)
            if email and throttle.pilot.hit("forgot_email", email):
                return h._json(202, {"status": "check_email"})
            return h._json(202, request_password_reset(conn, body.get("email"), ip=ip, ua=ua,
                                                       purpose=auth_tokens.PILOT_RESET))
        if path == "/api/pilot/auth/reset":
            if throttle.pilot.hit("reset_ip", str(ip or "unknown")):
                raise PilotError("too_many_requests", status=429)
            try:
                complete_password_reset(conn, body.get("token"), body.get("password"),
                                        purpose=auth_tokens.PILOT_RESET, ip=ip, ua=ua)
            except AuthError as exc:
                raise PilotError("invalid_token" if exc.status == 400 else "invalid_login", status=exc.status)
            return h._json(200, {"ok": True})

    # ---- authenticated ------------------------------------------------------
    principal = accounts.principal_for_token(conn, platform, token)
    if method == "GET" and path == "/api/pilot/session":
        # Page-load probe: answers 200 either way so a signed-out visitor
        # produces no error; private data is returned only when signed in.
        if principal is None:
            return h._json(200, {"signed_in": False})
        return h._json(200, dict(accounts.me(conn, platform, principal), signed_in=True))
    if principal is None:
        raise PilotError("authentication_required", status=401)

    if method == "POST" and path == "/api/pilot/auth/logout":
        logout(conn, token, user_id=principal.user_id, ip_address=ip, user_agent=ua)
        return h._json(200, {"ok": True}, raw_cookie=_cookie(h, None))
    if method == "GET" and path == "/api/pilot/me":
        return h._json(200, accounts.me(conn, platform, principal))
    if method == "PATCH" and path == "/api/pilot/me/language":
        return h._json(200, {"language": accounts.set_language(conn, principal, body.get("language"))})
    if method == "POST" and path == "/api/pilot/auth/request-verification":
        if throttle.pilot.hit("verify_ip", str(ip or "unknown")):
            raise PilotError("too_many_requests", status=429)
        email = normalize_email((principal.user or {}).get("email") or "")
        if email and throttle.pilot.hit("verify_email", email):
            return h._json(202, {"status": "check_email"})
        return h._json(202, accounts.request_legacy_verification(conn, principal, ip=ip, ua=ua))
    if method == "POST" and path == "/api/pilot/auth/verify-email":
        return h._json(200, accounts.complete_legacy_verification(conn, principal, body.get("token"), ip=ip, ua=ua))

    # contractor
    if method == "PATCH" and path == "/api/pilot/contractor/profile":
        return h._json(200, accounts.update_profile(conn, platform, principal, body))
    if method == "GET" and path == "/api/pilot/contractor/connections":
        return h._json(200, {"items": accounts.list_connections(conn, platform, principal)})
    if method == "POST" and path == "/api/pilot/contractor/connections":
        return h._json(200, accounts.connect_by_referral(conn, platform, principal, body.get("referral_code")))
    if method == "POST" and path == "/api/pilot/contractor/lines/parse":
        accounts.require_contractor(principal)
        return h._json(200, {"lines": materials.parse_pasted(body.get("text"))})
    if path == "/api/pilot/contractor/requests":
        if method == "GET":
            return h._json(200, {"items": materials.list_requests(conn, platform, principal)})
        if method == "POST":
            return h._json(201, materials.create_request(conn, platform, principal, body))
    m = _REQ.match(path)
    if m:
        rid, send = m.group(1), m.group(2)
        if method == "GET" and not send:
            return h._json(200, materials.get_request(conn, platform, principal, rid))
        if method == "PATCH" and not send:
            return h._json(200, materials.update_request(conn, platform, principal, rid, body))
        if method == "POST" and send:
            return h._json(200, materials.send_request(conn, platform, principal, rid, body))

    # supplier
    if method == "GET" and path == "/api/pilot/supplier/requests":
        return h._json(200, {"items": materials.inbox(conn, principal)})
    m = _SHARE.match(path)
    if m:
        sid, ack = m.group(1), m.group(2)
        if method == "GET" and not ack:
            return h._json(200, materials.inbox_detail(conn, principal, sid))
        if method == "POST" and ack:
            return h._json(200, materials.acknowledge(conn, principal, sid))

    raise PilotError("not_found", status=404)


def _client_event(h, platform, conn, token, body):
    event = body.get("event")
    if event not in analytics.CLIENT_EVENTS:
        raise PilotError("invalid_event")
    referral = None
    if body.get("referral_code"):
        referral = accounts.resolve_referral(conn, platform, body.get("referral_code"))
    principal = accounts.principal_for_token(conn, platform, token) if token else None
    if principal is not None and principal.kind == "contractor":
        profile = accounts.contractor_profile(conn, principal.tenant_id)
        analytics.record_for_contractor(conn, event, profile, user_id=principal.user_id,
                                        visit_id=body.get("visit_id"), page=body.get("page"))
    else:
        utm = analytics.clean_utm(body.get("utm"))
        analytics.record(conn, event, visit_id=body.get("visit_id"),
                         referral_code=referral["code"] if referral else None,
                         supplier_tenant_id=referral["_supplier_tenant_id"] if referral else None,
                         utm=utm, page=body.get("page"))
    return h._json(202, {"ok": True})
