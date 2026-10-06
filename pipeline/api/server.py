"""Permission-enforcing API + employee portal server.

    python -m pipeline.api.server

Every /api route (except login) requires a valid session. Authorization,
organization boundaries, and record-level company access are enforced in the
backend service layer — never in the browser. Restricted fields are removed by
serializers before responses leave the server.
"""

from __future__ import annotations

import json
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from pipeline.auth.rbac import AuthzError
from pipeline.billing import contractor_accounts
from pipeline.billing import health as billing_health
from pipeline.billing import service as billing
from pipeline.billing.config import billing_enabled, load_config as load_billing_config
from pipeline.billing.gateway import default_gateway as default_billing_gateway
from pipeline.billing.webhook import MAX_PAYLOAD_BYTES as BILLING_MAX_WEBHOOK_BYTES
from pipeline.billing.webhook import handle_webhook as handle_billing_webhook
from pipeline.auth import throttle
from pipeline.auth import tokens as auth_tokens
from pipeline.auth.service import (
    AuthError,
    change_password,
    complete_password_reset,
    get_current_user,
    login,
    logout,
    request_password_reset,
    serialize_user_context,
    write_audit,
)
from pipeline.config import settings
from pipeline.crm import admin as crm_admin
from pipeline.crm import service as crm
from pipeline.crm.service import ValidationError
from pipeline.products import service as products
from pipeline.reports import catalog as reports_catalog
from pipeline import pipeline_runs
from pipeline.db.database import get_connection, init_db

import os

# Bind address/port. Defaults are unchanged (loopback only); a hosted
# deployment sets these explicitly, normally still behind a reverse proxy.
HOST = os.environ.get("CORRIDORIQ_HOST", "127.0.0.1")
PORT = int(os.environ.get("CORRIDORIQ_PORT", settings.SALES_API_PORT))
COOKIE = settings.SESSION_COOKIE_NAME

# Portal files served same-origin (allowlist by extension + known names).
_STATIC_SUFFIXES = (".html", ".js", ".css")

# Public site entry point ("/" and the old "/index.html").
_HOME_PAGE = "landing.html"

# Legacy static pages from the original pitch site. They read files this server
# never serves (data/exports/*.json) or a different API, so they are not served.
_LEGACY_UNSERVED = {
    "dashboard.html", "dashboard.js", "contractors.html", "contractors.js",
    "companies.html", "companies.js", "company-profile.html", "company-profile.js",
    "job.html", "job.js", "knowledge.html", "knowledge.js", "script.js", "style.css",
    # Previous homepage, replaced by landing.html at "/"; kept in the repo, not served.
    "home.html",
}

# Files below the project root are served only from these folders, and only
# with these types. Everything else in a subdirectory is never served.
_ASSET_TYPES = {
    ".css": "text/css", ".js": "application/javascript",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
}
_SUBDIR_RULES = {
    "assets": set(_ASSET_TYPES),
    # Front-end sample data for the procurement preview. Fictional by design.
    "demo": {".js"},
}
_PORTAL_PAGES = {
    "login.html", "sales-dashboard.html", "sales-dashboard.js",
    "my-companies.html", "my-companies.js", "sales-company-profile.html",
    "sales-company-profile.js", "my-tasks.html", "my-tasks.js",
    "team-dashboard.html", "team-dashboard.js", "user-management.html",
    "user-management.js",
    # Role-aware dashboards.
    "admin-dashboard.html", "admin-dashboard.js",
    "ceo-morning-brief.html", "ceo-morning-brief.js",
    "estimator-work-queue.html", "estimator-work-queue.js",
    "readonly-dashboard.html", "readonly-dashboard.js",
    # Sprint 6 — product pricing pages.
    "product-search.html", "product-search.js", "quote-compare.html",
    "quote-compare.js", "catalog-admin.html", "catalog-admin.js",
    # Sales workspace redesign pages.
    "opportunities.html", "opportunities.js", "activity.html", "activity.js",
    "reports.html", "reports.js", "assignments.html", "assignments.js",
    "portal.css",
}

# Supplier billing (Stripe). Off unless CORRIDORIQ_BILLING_ENABLED=1; while
# off, the billing page and every /api/billing route answer 404.
_BILLING_PAGES = {"billing.html", "billing.js", "contractor-accounts.html", "contractor-accounts.js"}
_BILLING_PREFIX = "/api/billing/"
_BILLING_WEBHOOK = "/api/billing/stripe/webhook"
_BILLING_MAX_ACTION_BODY = 16 * 1024

# Contractor pilot referral entry: /join/<public referral code>.
_JOIN_RE = re.compile(r"^/join/[a-z0-9-]{4,40}/?$")
_JOIN_PAGE = "contractor-join.html"

_ID = r"(\d+)"
_SALES_COMPANY_RE = re.compile(
    rf"^/api/sales/companies/{_ID}(?:/(projects|permits|activities|relationship))?$")
_SALES_TASK_RE = re.compile(rf"^/api/sales/tasks/{_ID}$")
_ADMIN_USER_RE = re.compile(rf"^/api/admin/users/{_ID}$")


def _factory():
    """Connection factory (overridable in tests)."""
    return get_connection()


def _billing_gateway(config):
    """Stripe gateway factory (overridable in tests, which never reach Stripe)."""
    return default_billing_gateway(config)


def static_target(path: str):
    """(file, content type) for a static request, or (None, None).

    Top level: .html/.js/.css only, excluding the legacy pitch-site pages.
    Subdirectories: only those in _SUBDIR_RULES, only their allowed types.
    The resolved file must stay inside the project root."""
    from urllib.parse import unquote

    name = unquote(path).lstrip("/")
    if name in ("", "index.html"):
        name = _HOME_PAGE
    if "\\" in name or "\x00" in name:
        return None, None
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        return None, None
    suffix = os.path.splitext(name)[1].lower()
    if len(parts) == 1:
        if name in _LEGACY_UNSERVED:
            return None, None
        if name in _BILLING_PAGES and not billing_enabled():
            return None, None
        if name not in _PORTAL_PAGES and suffix not in _STATIC_SUFFIXES:
            return None, None
        ctype = ("text/html; charset=utf-8" if suffix == ".html"
                 else "application/javascript" if suffix == ".js" else "text/css")
    else:
        allowed = _SUBDIR_RULES.get(parts[0])
        if not allowed or suffix not in allowed:
            return None, None
        ctype = _ASSET_TYPES[suffix]
    root = settings.PROJECT_ROOT.resolve()
    target = (root / name).resolve()
    # Prevent path traversal outside the project root.
    if root not in target.parents or not target.is_file():
        return None, None
    return target, ctype


def _launch_morning_refresh(user_id: int) -> None:
    """Run the morning refresh in a background thread with its own connection so
    the HTTP request returns immediately. Duplicate runs are refused by the
    pipeline lock inside run_morning_refresh."""
    import threading

    def _worker():
        conn = _factory()
        try:
            pipeline_runs.run_morning_refresh(
                conn=conn, trigger_source="manual", triggered_by=user_id)
        except pipeline_runs.RunInProgressError:
            pass
        except Exception:  # pragma: no cover - background best-effort
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    threading.Thread(target=_worker, daemon=True).start()


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "CorridorIQ/1.0"
    sys_version = ""  # do not advertise the Python version

    def log_message(self, *args):  # quieter console
        pass

    # ---- response helpers -------------------------------------------------
    def _json(self, code: int, payload, *, set_cookie: str | None = None,
              clear_cookie: bool = False, raw_cookie: str | None = None):
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        if set_cookie is not None:
            self.send_header("Set-Cookie", self._cookie_header(set_cookie))
        if clear_cookie:
            self.send_header("Set-Cookie",
                             f"{COOKIE}=; Path=/; HttpOnly; Max-Age=0; SameSite=Lax")
        if raw_cookie is not None:
            self.send_header("Set-Cookie", raw_cookie)
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, target):
        """Stream a generated report file for download."""
        import mimetypes
        data = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Disposition", f'attachment; filename="{target.name}"')
        self.end_headers()
        self.wfile.write(data)

    def _cookie_header(self, token: str) -> str:
        parts = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Lax",
                 f"Max-Age={settings.SESSION_TTL_HOURS * 3600}"]
        if settings.AUTH_PRODUCTION:
            parts.append("Secure")
        return "; ".join(parts)

    # ---- request helpers --------------------------------------------------
    def _token(self, name: str = COOKIE) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        jar = SimpleCookie()
        try:
            jar.load(raw)
        except Exception:
            return None
        morsel = jar.get(name)
        return morsel.value if morsel else None

    def _pilot_token(self) -> str | None:
        from pipeline.pilot.platform import PILOT_COOKIE
        return self._token(PILOT_COOKIE)

    def _is_json(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw or b"{}")
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _client(self):
        from pipeline.api.client_address import resolve_client_ip
        peer = self.client_address[0] if self.client_address else None
        ip = resolve_client_ip(peer, self.headers)
        ua = self.headers.get("User-Agent")
        return ip, ua

    def _flatten(self, qs: dict) -> dict:
        return {k: (v[0] if isinstance(v, list) and v else v) for k, v in qs.items()}

    def _require_user(self, conn):
        user = get_current_user(conn, self._token())
        if user is None:
            self._json(401, {"error": "authentication required"})
            return None
        return user

    def _deny(self, conn, user, path, message):
        """Record a permission_denied event, then return 403."""
        try:
            write_audit(conn, event_type="permission_denied", success=False,
                        user_id=user["id"] if user else None,
                        organization_id=user["organization_id"] if user else None,
                        resource_type="route", resource_id=path, details={"error": message})
        except Exception:
            pass

    # ---- static portal ----------------------------------------------------
    def _serve_static(self, path: str) -> bool:
        target, ctype = static_target(path)
        if target is None:
            return False
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        # Fonts and images are immutable per deploy; pages and code revalidate.
        if ctype.startswith(("image/", "font/")):
            self.send_header("Cache-Control", "public, max-age=86400")
        else:
            self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)
        return True

    # ---- dispatch ---------------------------------------------------------
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        if not path.startswith("/api/"):
            # Supplier referral links (/join/<code>) open the contractor join
            # page; the page reads the code from the URL.
            if _JOIN_RE.match(path) and self._serve_static("/" + _JOIN_PAGE):
                return
            if self._serve_static(path):
                return
            return self._json(404, {"error": "not found"})
        if path.startswith(_BILLING_PREFIX):
            return self._billing_dispatch("GET", path)
        query = self._flatten(parse_qs(parsed.query))
        if path.startswith("/api/pilot/"):
            # Contractor pilot: separate platform database; the intelligence
            # database is never opened for these routes.
            from pipeline.pilot import api as pilot_api
            return pilot_api.handle(self, "GET", path, query, None)
        conn = _factory()
        try:
            self._route_get(conn, path, query)
        except AuthzError as exc:
            self._deny(conn, getattr(self, "_cur_user", None), path, str(exc))
            self._json(exc.status, {"error": str(exc)})
        except (ValidationError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:  # pragma: no cover
            # Never echo internals (paths, SQL, stack detail) to the client.
            self._json(500, {"error": "internal error"})
        finally:
            conn.close()

    def do_POST(self):
        self._write_dispatch("POST")

    def do_PATCH(self):
        self._write_dispatch("PATCH")

    def _write_dispatch(self, method):
        parsed = urlparse(self.path)
        path = parsed.path
        if path.startswith(_BILLING_PREFIX):
            return self._billing_dispatch(method, path)
        if path.startswith("/api/pilot/"):
            from pipeline.pilot import api as pilot_api
            return pilot_api.handle(self, method, path, {}, self._body())
        conn = _factory()
        try:
            body = self._body()
            self._route_write(conn, method, path, body)
        except AuthError as exc:
            self._json(exc.status, {"error": str(exc)})
        except AuthzError as exc:
            self._deny(conn, getattr(self, "_cur_user", None), path, str(exc))
            self._json(exc.status, {"error": str(exc)})
        except (ValidationError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
        except Exception:  # pragma: no cover
            # Never echo internals (paths, SQL, stack detail) to the client.
            self._json(500, {"error": "internal error"})
        finally:
            conn.close()

    # ---- billing (Stripe) -------------------------------------------------
    def _billing_dispatch(self, method, path):
        """/api/billing/*: status, checkout and portal need a session; the
        webhook is authenticated by its Stripe signature instead."""
        if not billing_enabled():
            return self._json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if path == _BILLING_WEBHOOK:
            if method != "POST":
                return self._json(405, {"error": "method not allowed"})
            if length <= 0 or length > BILLING_MAX_WEBHOOK_BYTES:
                return self._json(400, {"error": "invalid payload"})
            raw = self.rfile.read(length)
            config = load_billing_config()
            gateway = None if config.webhook_problems() else _billing_gateway(config)
            conn = _factory()
            try:
                status, payload = handle_billing_webhook(
                    conn, raw, self.headers.get("Stripe-Signature"), config, gateway)
            except Exception:  # pragma: no cover - never echo internals
                status, payload = 500, {"error": "internal error"}
            finally:
                conn.close()
            return self._json(status, payload)

        if length > _BILLING_MAX_ACTION_BODY:
            return self._json(413, {"error": "request too large"})
        if length:
            self.rfile.read(length)  # ignored: the server decides every billing parameter
        actions = {("GET", "/api/billing/status"), ("POST", "/api/billing/checkout"),
                   ("POST", "/api/billing/portal")}
        if (method, path) not in actions:
            return self._json(404, {"error": "unknown route"})
        if method == "POST":
            # CSRF hardening on top of SameSite=Lax: a cross-site HTML form
            # cannot send application/json without a CORS preflight.
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._json(415, {"error": "unsupported media type"})
        conn = _factory()
        try:
            user = self._require_user(conn)
            if user is None:
                return
            if user.get("must_change_password"):
                return self._json(403, {"error": "password_change_required"})
            self._cur_user = user
            ip, ua = self._client()
            config = load_billing_config()
            if path == "/api/billing/status":
                return self._json(200, billing.billing_status(conn, user, config))
            gateway = _billing_gateway(config) if not config.problems() else None
            if path == "/api/billing/checkout":
                return self._json(200, billing.start_checkout(conn, user, config, gateway, ip=ip, ua=ua))
            return self._json(200, billing.create_portal_session(conn, user, config, gateway, ip=ip, ua=ua))
        except AuthzError as exc:
            self._deny(conn, getattr(self, "_cur_user", None), path, str(exc))
            self._json(exc.status, {"error": str(exc)})
        except billing.BillingError as exc:
            self._json(exc.status, {"error": str(exc)})
        except Exception:  # pragma: no cover - fail closed, never echo internals
            self._json(500, {"error": "internal error"})
        finally:
            conn.close()

    # ---- GET routes -------------------------------------------------------
    def _route_get(self, conn, path, query):
        if path == "/api/health":
            return self._json(200, {"ok": True, "service": "corridoriq-sales"})
        if path == "/api/auth/me":
            user = self._require_user(conn)
            if user is None:
                return
            return self._json(200, {"user": serialize_user_context(user)})

        user = self._require_user(conn)
        if user is None:
            return
        if user.get("must_change_password"):
            return self._json(403, {"error": "password_change_required"})
        self._cur_user = user
        ip, ua = self._client()

        if path == "/api/sales/dashboard":
            return self._json(200, crm.dashboard(conn, user))
        if path == "/api/admin/dashboard":
            return self._json(200, crm_admin.admin_dashboard(conn, user))
        if path == "/api/estimator/work-queue":
            return self._json(200, crm_admin.estimator_work_queue(conn, user))
        if path == "/api/sales/companies":
            return self._json(200, crm.list_my_companies(conn, user, query))
        if path == "/api/sales/opportunities":
            return self._json(200, crm.opportunities(conn, user, query))
        if path == "/api/sales/opportunities/map":
            return self._json(200, crm.opportunity_map(conn, user, query))
        if path == "/api/sales/activity":
            return self._json(200, crm.my_activity(conn, user, query))
        if path == "/api/sales/followups":
            include_future = str(query.get("scope", "")).lower() != "due"
            return self._json(200, {"items": crm.followups_due(
                conn, user, include_future=include_future, limit=200)})
        if path == "/api/sales/tasks":
            return self._json(200, {"items": crm.list_tasks(conn, user, query)})
        if path == "/api/manager/team":
            return self._json(200, {"items": crm_admin.team_overview(conn, user)})
        if path == "/api/manager/assignments":
            return self._json(200, {"items": crm.list_assignments(conn, user)})
        if path == "/api/admin/users":
            return self._json(200, {"items": crm_admin.list_users(conn, user)})

        # --- billing health / operator incidents (read-only, local state only) ---
        if path == "/api/admin/billing/health":
            from pipeline.auth.rbac import require_permission
            require_permission(user, "pipeline.monitor")
            return self._json(200, billing_health.billing_health(conn, load_billing_config()))
        if path == "/api/admin/billing/incidents":
            contractor_accounts._require_operator(conn, user)
            return self._json(200, {"items": billing_health.billing_incidents(conn)})
        if path == "/api/admin/contractor-accounts":
            if not billing_enabled():
                return self._json(404, {"error": "unknown route"})
            return self._json(200, {"items": contractor_accounts.list_contractor_accounts(
                conn, user, load_billing_config())})

        # --- data pipeline / morning refresh status ---
        if path == "/api/status/refresh":
            # Simple, non-technical status for every authenticated user.
            return self._json(200, pipeline_runs.employee_status(conn))
        if path == "/api/admin/morning-refresh":
            from pipeline.auth.rbac import require_permission
            require_permission(user, "pipeline.monitor")
            return self._json(200, pipeline_runs.admin_status(conn))
        if path == "/api/admin/ceo-morning-brief":
            from pipeline.auth.rbac import require_permission
            from agents.ceo.analytics.serve import load_owner_brief_view
            from agents.ceo.paths import output_dir
            require_permission(user, "admin.system")
            return self._json(200, load_owner_brief_view(
                reports_conn=conn, directory=output_dir() / "intelligence"))

        # --- reports ---
        if path == "/api/reports/catalog":
            return self._json(200, reports_catalog.catalog(conn, user))
        if path == "/api/reports/download":
            from pipeline.auth.rbac import require_permission
            require_permission(user, "reports.view")
            target = reports_catalog.safe_generated_path(query.get("name", ""))
            if target is None:
                return self._json(404, {"error": "report not found"})
            return self._send_file(target)

        # --- products (Sprint 6) ---
        if path == "/api/products/search":
            return self._json(200, products.search(conn, user, query))
        if path == "/api/admin/suppliers":
            return self._json(200, {"items": products.list_suppliers(conn, user)})
        if path == "/api/admin/catalog/imports":
            return self._json(200, {"items": products.import_history(conn, user)})
        if path == "/api/admin/pricing/delivery":
            return self._json(200, {"config": products.get_delivery_settings(conn, user)})

        m = _SALES_COMPANY_RE.match(path)
        if m:
            cid, sub = int(m.group(1)), m.group(2)
            if sub is None:
                return self._json(200, crm.get_company_detail(conn, user, cid, ip=ip, ua=ua))
            if sub == "projects":
                return self._json(200, {"items": crm.get_company_projects(conn, user, cid)})
            if sub == "permits":
                return self._json(200, {"items": crm.get_company_permits(conn, user, cid)})
            if sub == "activities":
                return self._json(200, {"items": crm.list_activities(conn, user, cid)})
            if sub == "relationship":
                return self._json(200, {"relationship": crm.get_relationship(conn, user, cid)})
        return self._json(404, {"error": "unknown route"})

    # ---- write routes -----------------------------------------------------
    def _route_write(self, conn, method, path, body):
        ip, ua = self._client()

        # --- auth (login / recovery / invitation are unauthenticated writes) ---
        if method == "POST" and path == "/api/auth/login":
            if throttle.portal.hit("login_ip", str(ip or "unknown")):
                return self._json(429, {"error": "too_many_requests"})
            token, user = login(conn, body.get("email", ""), body.get("password", ""),
                                ip_address=ip, user_agent=ua)
            return self._json(200, {"user": serialize_user_context(user)}, set_cookie=token)
        if method == "POST" and path == "/api/auth/forgot":
            email = (body.get("email") or "").strip().lower()
            if throttle.portal.hit("forgot_ip", str(ip or "unknown")):
                return self._json(429, {"error": "too_many_requests"})
            if email and throttle.portal.hit("forgot_email", email):
                return self._json(202, {"status": "check_email"})
            return self._json(202, request_password_reset(conn, body.get("email"), ip=ip, ua=ua,
                                                          purpose=auth_tokens.PORTAL_RESET))
        if method == "POST" and path == "/api/auth/reset":
            if throttle.portal.hit("reset_ip", str(ip or "unknown")):
                return self._json(429, {"error": "too_many_requests"})
            complete_password_reset(conn, body.get("token"), body.get("password"),
                                    purpose=auth_tokens.PORTAL_RESET, ip=ip, ua=ua)
            return self._json(200, {"ok": True})
        if method == "POST" and path == "/api/auth/accept-invite":
            if throttle.portal.hit("invite_accept_ip", str(ip or "unknown")):
                return self._json(429, {"error": "too_many_requests"})
            return self._json(200, contractor_accounts.accept_owner_invitation(
                conn, body.get("token"), body.get("password"), ip=ip, ua=ua))

        user = self._require_user(conn)
        if user is None:
            return
        if user.get("must_change_password") and path not in ("/api/auth/change-password", "/api/auth/logout"):
            return self._json(403, {"error": "password_change_required"})
        self._cur_user = user

        if method == "POST" and path == "/api/auth/logout":
            token = self._token()
            logout(conn, token, user_id=user["id"], ip_address=ip, user_agent=ua)
            return self._json(200, {"ok": True}, clear_cookie=True)
        if method == "POST" and path == "/api/auth/change-password":
            change_password(conn, user["id"], body.get("old_password", ""),
                            body.get("new_password", ""), ip_address=ip, user_agent=ua)
            # Password change revokes sessions; force re-login.
            return self._json(200, {"ok": True}, clear_cookie=True)

        # --- sales ---
        m = _SALES_COMPANY_RE.match(path)
        if m:
            cid, sub = int(m.group(1)), m.group(2)
            if method == "POST" and sub == "activities":
                return self._json(201, crm.create_activity(conn, user, cid, body, ip=ip, ua=ua))
            if method == "PATCH" and sub == "relationship":
                return self._json(200, crm.update_relationship(conn, user, cid, body, ip=ip, ua=ua))

        if method == "POST" and path == "/api/sales/tasks":
            return self._json(201, crm.create_task(conn, user, body, ip=ip, ua=ua))
        m = _SALES_TASK_RE.match(path)
        if m and method == "PATCH":
            return self._json(200, crm.update_task(conn, user, int(m.group(1)), body, ip=ip, ua=ua))

        # --- manager ---
        if method == "POST" and path == "/api/manager/assignments":
            return self._json(200, crm.assign_company(
                conn, user, int(body.get("company_id")), int(body.get("new_user_id")),
                reason=body.get("reason"), ip=ip, ua=ua))

        # --- admin ---
        if method == "POST" and path == "/api/admin/morning-refresh/run":
            from pipeline.auth.rbac import require_permission
            require_permission(user, "pipeline.run")
            if pipeline_runs.is_running(conn):
                return self._json(409, {"error": "A refresh is already in progress."})
            _launch_morning_refresh(user["id"])
            return self._json(202, {"ok": True, "message": "Morning refresh started."})
        if method == "POST" and path == "/api/admin/users":
            return self._json(201, crm_admin.create_user(conn, user, body, ip=ip, ua=ua))
        if method == "POST" and path == "/api/admin/contractor-accounts":
            if not billing_enabled():
                return self._json(404, {"error": "unknown route"})
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                return self._json(415, {"error": "unsupported media type"})
            try:
                return self._json(201, contractor_accounts.create_contractor_account(conn, user, body, ip=ip, ua=ua))
            except contractor_accounts.ContractorAccountError as exc:
                return self._json(400, {"error": "invalid contractor account details", "fields": exc.errors})
        m = _ADMIN_USER_RE.match(path)
        if m and method == "PATCH":
            return self._json(200, crm_admin.update_user(conn, user, int(m.group(1)), body, ip=ip, ua=ua))

        # --- products (Sprint 6) ---
        if method == "POST" and path == "/api/products/quote":
            return self._json(200, products.quote(
                conn, user, body.get("product_id"), body.get("quantity", 1),
                body.get("jobsite") or {}, ip=ip, ua=ua))
        if method == "POST" and path == "/api/admin/suppliers":
            return self._json(201, products.add_supplier(conn, user, body, ip=ip, ua=ua))
        if method == "POST" and path == "/api/admin/catalog/preview":
            return self._json(200, products.preview_catalog(conn, user, body))
        if method == "POST" and path == "/api/admin/catalog/import":
            return self._json(200, products.import_catalog_upload(conn, user, body, ip=ip, ua=ua))
        if method in ("PATCH", "POST") and path == "/api/admin/pricing/delivery":
            return self._json(200, {"config": products.update_delivery_settings(
                conn, user, body.get("updates") or body, ip=ip, ua=ua)})

        return self._json(404, {"error": "unknown route"})


def main():
    init_db()  # ensure schema + seed once at startup
    server = ThreadingHTTPServer((HOST, PORT), ApiHandler)
    # Pre-compute Today's Accounts and the organization opportunity scan after
    # each refresh (and at startup) so the first dashboard visit is not cold.
    from pipeline.api import warmer
    if warmer.enabled():
        warmer.CacheWarmer(settings.DB_PATH).start()
    print(f"CorridorIQ secure portal + API on http://{HOST}:{PORT}")
    print(f"  Portal login:  http://{HOST}:{PORT}/login.html")
    print("  POST /api/auth/login | logout | change-password ; GET /api/auth/me")
    print("  GET  /api/sales/dashboard | companies | companies/<id>[/projects|permits|activities]")
    print("  POST /api/sales/companies/<id>/activities ; PATCH .../relationship")
    print("  GET/POST /api/sales/tasks ; PATCH /api/sales/tasks/<id>")
    print("  GET /api/manager/team|assignments ; POST /api/manager/assignments")
    print("  GET/POST /api/admin/users ; PATCH /api/admin/users/<id>")
    print("  GET /api/products/search ; POST /api/products/quote")
    print("  GET/POST /api/admin/suppliers ; POST /api/admin/catalog/preview|import")
    print("  GET /api/admin/catalog/imports ; GET/PATCH /api/admin/pricing/delivery")
    print("  GET /api/status/refresh ; GET /api/admin/morning-refresh ; "
          "POST /api/admin/morning-refresh/run")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
