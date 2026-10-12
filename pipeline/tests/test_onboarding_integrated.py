"""Self-service contractor/supplier onboarding on the production identity stack.

Ports the intent of the frictionless-onboarding branch tests (registration,
verification gates, supplier approval, employee isolation) and adds the
integration guarantees: production token store, production mail transports,
fragment links, client-address attribution and shared-hop throttling.
"""
from __future__ import annotations

import http.client
import json
import sqlite3
import ssl
import threading
import time
from urllib.parse import urlsplit

import pytest

from pipeline.api import client_address
from pipeline.auth import mail, onboarding, readiness, throttle, tokens
from pipeline.auth import service as auth
from pipeline.auth.seed import ensure_default_organization, seed_auth
from pipeline.config.settings import SCHEMA_PATH

ADMIN = ("onb-admin@corridoriq.test", "AdminPass123")
MANAGER = ("onb-manager@corridoriq.test", "ManagerPass123")
REP = ("onb-rep@corridoriq.test", "RepPass12345")
PW = "Contractor-Pass-1"


# ---- fixtures ----------------------------------------------------------------------

@pytest.fixture()
def site(tmp_path, monkeypatch):
    import pipeline.api.server as server_mod
    from http.server import ThreadingHTTPServer

    db_file = tmp_path / "onboarding.db"

    def factory():
        c = sqlite3.connect(db_file)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(c)
    org = ensure_default_organization(c)
    ids = {
        "admin": auth.create_user(c, organization_id=org, email=ADMIN[0], password=ADMIN[1],
                                  role_names=["admin"], must_change_password=False),
        "manager": auth.create_user(c, organization_id=org, email=MANAGER[0], password=MANAGER[1],
                                    role_names=["sales_manager"], must_change_password=False),
        "rep": auth.create_user(c, organization_id=org, email=REP[0], password=REP[1],
                                role_names=["sales_representative"], must_change_password=False),
    }
    c.commit()
    c.close()

    monkeypatch.delenv(mail.ENV_PROVIDER, raising=False)
    monkeypatch.delenv(client_address.SOURCE_ENV, raising=False)
    monkeypatch.setattr(server_mod, "_factory", factory)
    mail.sink().clear()
    throttle.portal.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    yield {"port": srv.server_address[1], "db": factory, "ids": ids}
    srv.shutdown()
    throttle.portal.clear()


def _req(site, method, path, body=None, cookie=None, ctype="application/json", raw=None):
    conn = http.client.HTTPConnection("127.0.0.1", site["port"], timeout=10)
    headers = {}
    if ctype:
        headers["Content-Type"] = ctype
    if cookie:
        headers["Cookie"] = cookie
    data = raw if raw is not None else (json.dumps(body) if body is not None else None)
    conn.request(method, path, data, headers)
    resp = conn.getresponse()
    payload = resp.read()
    cookie_out = resp.getheader("Set-Cookie")
    status = resp.status
    conn.close()
    try:
        out = json.loads(payload) if payload else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        out = payload.decode("utf-8", "replace")
    return status, out, (cookie_out.split(";")[0] if cookie_out else None)


def _login(site, email, password):
    status, body, cookie = _req(site, "POST", "/api/auth/login", {"email": email, "password": password})
    assert status == 200, body
    return cookie


def _register(site, kind="contractor", email="new.contractor@example.test", **extra):
    body = {"name": "Casey Builder", "business_name": "Casey Builds LLC", "email": email, "password": PW}
    if kind == "supplier":
        body.update(phone="602-555-0100", business_category="plumbing", name="Sam Supply")
    body.update(extra)
    return _req(site, "POST", f"/api/auth/register/{kind}", body)


def _verify_link(email, kind=mail.PORTAL_VERIFY):
    msgs = mail.sink().outbox(to=email, kind=kind)
    assert msgs, f"no {kind} message for {email}"
    return msgs[-1].link


def _fragment_token(link):
    parts = urlsplit(link)
    assert "token=" not in parts.query, "one-time tokens must not be in the query string"
    assert parts.fragment.startswith("t=")
    return parts.fragment[2:]


def _verify(site, email):
    return _req(site, "POST", "/api/auth/verify-email", {"token": _fragment_token(_verify_link(email))})


def _user_row(site, email):
    c = site["db"]()
    try:
        return c.execute("SELECT * FROM users WHERE normalized_email=?", (email,)).fetchone()
    finally:
        c.close()


# ---- registration -------------------------------------------------------------------

def test_contractor_registration_creates_signed_in_unverified_account(site):
    status, body, cookie = _register(site)
    assert status == 201 and cookie, body
    user = body["user"]
    assert user["account_kind"] == "contractor" and user["roles"] == ["contractor"]
    assert user["email_verified"] is False and body["verification_sent"] is True
    assert user["default_landing_page"] == "contractor-welcome.html"
    link = _verify_link("new.contractor@example.test")
    assert "/verify-email.html?mode=verify#t=" in link
    row = _user_row(site, "new.contractor@example.test")
    assert row["account_state"] == "ACTIVE" and row["must_change_password"] == 0
    assert row["password_hash"].startswith("$argon2")


def test_supplier_registration_requires_phone_and_category_and_starts_pending(site):
    status, body, _ = _register(site, "supplier", email="sup@example.test", phone="")
    assert status == 400
    status, body, _ = _register(site, "supplier", email="sup@example.test", business_category="cars")
    assert status == 400
    status, body, cookie = _register(site, "supplier", email="sup@example.test")
    assert status == 201 and cookie
    assert body["user"]["account_state"] == onboarding.STATE_PENDING_EMAIL
    assert body["user"]["default_landing_page"] == "supplier-welcome.html"


def test_registration_ignores_role_and_kind_injection(site):
    status, body, _ = _register(site, email="sneaky@example.test", roles=["admin"],
                                account_kind="employee", account_state="ACTIVE", role="admin")
    assert status == 201
    assert body["user"]["roles"] == ["contractor"]
    assert "admin.system" not in body["user"]["permissions"]
    assert body["user"]["account_kind"] == "contractor"


def test_admin_role_can_never_be_self_registered(site):
    c = site["db"]()
    with pytest.raises(ValueError):
        auth.create_user(c, organization_id=ensure_default_organization(c), email="x@example.test",
                         password=PW, role_names=["admin"], account_kind="contractor")
    c.close()


def test_registration_requires_json(site):
    status, _, _ = _req(site, "POST", "/api/auth/register/contractor",
                        raw="email=a%40b.test&password=x", ctype="application/x-www-form-urlencoded")
    assert status == 415


def test_duplicate_registration_is_generic_and_notifies_owner(site):
    assert _register(site, email="dup@example.test")[0] == 201
    status, body, cookie = _register(site, email="dup@example.test")
    assert status == 409 and cookie is None
    assert body["error"] == onboarding.GENERIC_REGISTER_ERROR
    notices = mail.sink().outbox(to="dup@example.test", kind=mail.PORTAL_ALREADY_REGISTERED)
    assert len(notices) == 1 and notices[0].link is None
    c = site["db"]()
    assert c.execute("SELECT COUNT(*) FROM users WHERE normalized_email='dup@example.test'").fetchone()[0] == 1
    c.close()


def test_duplicate_of_employee_email_does_not_touch_the_employee(site):
    status, _, _ = _register(site, email=ADMIN[0])
    assert status == 409
    assert _login(site, *ADMIN)


# ---- verification -------------------------------------------------------------------

def test_verification_link_is_single_use_and_unlocks_contractor_features(site):
    _, _, cookie = _register(site, email="v@example.test")
    assert _req(site, "GET", "/api/contractor/intelligence", cookie=cookie)[0] == 403
    token = _fragment_token(_verify_link("v@example.test"))
    status, body, _ = _req(site, "POST", "/api/auth/verify-email", {"token": token})
    assert status == 200 and body["user"]["email_verified"] is True
    assert _req(site, "POST", "/api/auth/verify-email", {"token": token})[0] == 400
    assert _req(site, "GET", "/api/contractor/intelligence", cookie=cookie)[0] == 200
    dash = _req(site, "GET", "/api/contractor/dashboard", cookie=cookie)[1]
    assert dash["email_verified"] is True and dash["locked"] == []


def test_verification_is_not_possible_with_a_get_request(site):
    _register(site, email="scanner@example.test")
    token = _fragment_token(_verify_link("scanner@example.test"))
    status, _, _ = _req(site, "GET", f"/api/auth/verify-email?token={token}")
    assert status in (401, 404)
    assert _user_row(site, "scanner@example.test")["email_verified_at"] is None


def test_verification_token_is_stored_hashed_with_production_schema(site):
    _register(site, email="hash@example.test")
    raw = _fragment_token(_verify_link("hash@example.test"))
    c = site["db"]()
    row = c.execute("SELECT * FROM auth_tokens WHERE purpose=?", (tokens.PORTAL_VERIFY,)).fetchone()
    assert row["token_hash"] == tokens.hash_token(raw)
    assert raw not in json.dumps(dict(row))
    assert row["email"] == "hash@example.test" and row["consumed_at"] is None
    c.close()


def test_resend_revokes_the_previous_link(site):
    _register(site, email="r@example.test")
    first = _fragment_token(_verify_link("r@example.test"))
    status, body, _ = _req(site, "POST", "/api/auth/resend-verification", {"email": "r@example.test"})
    assert status == 202 and body["message"] == onboarding.GENERIC_RESEND_ACK
    second = _fragment_token(_verify_link("r@example.test"))
    assert first != second
    assert _req(site, "POST", "/api/auth/verify-email", {"token": first})[0] == 400
    assert _req(site, "POST", "/api/auth/verify-email", {"token": second})[0] == 200


def test_resend_is_generic_for_unknown_verified_and_employee_accounts(site):
    _register(site, email="done@example.test")
    _verify(site, "done@example.test")
    before = len(mail.sink().outbox())
    for email in ("nobody@example.test", "done@example.test", ADMIN[0]):
        status, body, _ = _req(site, "POST", "/api/auth/resend-verification", {"email": email})
        assert status == 202 and body["message"] == onboarding.GENERIC_RESEND_ACK
    assert len(mail.sink().outbox()) == before


def test_email_change_invalidates_outstanding_verification(site):
    _register(site, email="old@example.test")
    token = _fragment_token(_verify_link("old@example.test"))
    admin = _login(site, *ADMIN)
    uid = _user_row(site, "old@example.test")["id"]
    status, _, _ = _req(site, "PATCH", f"/api/admin/users/{uid}", {"email": "new@example.test"}, cookie=admin)
    assert status == 200
    assert _req(site, "POST", "/api/auth/verify-email", {"token": token})[0] == 400
    assert _user_row(site, "new@example.test")["email_verified_at"] is None


# ---- supplier approval ---------------------------------------------------------------

def test_supplier_requires_verification_and_admin_approval(site):
    _, _, sup = _register(site, "supplier", email="s1@example.test")
    uid = _user_row(site, "s1@example.test")["id"]
    admin = _login(site, *ADMIN)
    assert _req(site, "GET", "/api/supplier/rfqs", cookie=sup)[0] == 403
    # Approval refused before the email is verified.
    assert _req(site, "POST", f"/api/admin/users/{uid}/approve-supplier", {}, cookie=admin)[0] == 409
    _verify(site, "s1@example.test")
    assert _user_row(site, "s1@example.test")["account_state"] == onboarding.STATE_PENDING_APPROVAL
    assert _req(site, "GET", "/api/supplier/rfqs", cookie=sup)[0] == 403
    status, body, _ = _req(site, "POST", f"/api/admin/users/{uid}/approve-supplier", {}, cookie=admin)
    assert status == 200 and body["user"]["account_state"] == "ACTIVE"
    assert _req(site, "GET", "/api/supplier/rfqs", cookie=sup)[0] == 200
    assert _req(site, "POST", f"/api/admin/users/{uid}/approve-supplier", {}, cookie=admin)[0] == 409


def test_supplier_approval_permissions_and_targets(site):
    _register(site, "supplier", email="s2@example.test")
    _verify(site, "s2@example.test")
    _register(site, email="c2@example.test")
    sup_id = _user_row(site, "s2@example.test")["id"]
    con_id = _user_row(site, "c2@example.test")["id"]
    manager = _login(site, *MANAGER)
    assert _req(site, "POST", f"/api/admin/users/{sup_id}/approve-supplier", {}, cookie=manager)[0] == 403
    admin = _login(site, *ADMIN)
    assert _req(site, "POST", f"/api/admin/users/{con_id}/approve-supplier", {}, cookie=admin)[0] == 400
    assert _req(site, "POST", f"/api/admin/users/{sup_id}/approve-supplier",
                raw="x", ctype="text/plain", cookie=admin)[0] == 415


def test_suspended_supplier_is_not_reenabled_by_approval(site):
    _, _, sup = _register(site, "supplier", email="s3@example.test")
    _verify(site, "s3@example.test")
    uid = _user_row(site, "s3@example.test")["id"]
    admin = _login(site, *ADMIN)
    status, body, _ = _req(site, "POST", f"/api/admin/users/{uid}/suspend", {}, cookie=admin)
    assert status == 200 and body["user"]["account_state"] == "SUSPENDED"
    assert _req(site, "GET", "/api/auth/me", cookie=sup)[0] == 401
    assert _req(site, "POST", f"/api/admin/users/{uid}/approve-supplier", {}, cookie=admin)[0] == 409
    row = _user_row(site, "s3@example.test")
    assert row["account_state"] == "SUSPENDED" and row["is_active"] == 0
    status, _, _ = _req(site, "POST", "/api/auth/login", {"email": "s3@example.test", "password": PW})
    assert status == 401


def test_suspend_rejects_employees_and_self(site):
    admin = _login(site, *ADMIN)
    assert _req(site, "POST", f"/api/admin/users/{site['ids']['rep']}/suspend", {}, cookie=admin)[0] == 400
    assert _req(site, "POST", f"/api/admin/users/{site['ids']['admin']}/suspend", {}, cookie=admin)[0] == 400


# ---- isolation from the employee workspace ------------------------------------------

EMPLOYEE_GETS = (
    "/api/sales/dashboard", "/api/admin/dashboard", "/api/estimator/work-queue",
    "/api/sales/companies", "/api/sales/opportunities", "/api/sales/tasks", "/api/manager/team",
    "/api/admin/users", "/api/status/refresh", "/api/reports/catalog", "/api/products/search",
    "/api/admin/suppliers", "/api/admin/mail-status", "/api/admin/morning-refresh",
)
EMPLOYEE_WRITES = (
    ("POST", "/api/sales/tasks"), ("POST", "/api/admin/users"),
    ("POST", "/api/products/quote"), ("POST", "/api/admin/users/1/approve-supplier"),
    ("POST", "/api/admin/users/1/suspend"), ("PATCH", "/api/admin/users/1"),
)


@pytest.mark.parametrize("kind", ["contractor", "supplier"])
def test_network_accounts_cannot_reach_employee_routes(site, kind):
    email = f"iso-{kind}@example.test"
    _, _, cookie = _register(site, kind, email=email)
    _verify(site, email)
    if kind == "supplier":
        admin = _login(site, *ADMIN)
        uid = _user_row(site, email)["id"]
        assert _req(site, "POST", f"/api/admin/users/{uid}/approve-supplier", {}, cookie=admin)[0] == 200
    for path in EMPLOYEE_GETS:
        assert _req(site, "GET", path, cookie=cookie)[0] == 403, path
    for method, path in EMPLOYEE_WRITES:
        assert _req(site, method, path, {}, cookie=cookie)[0] == 403, path


def test_contractor_and_supplier_workspaces_are_separate(site):
    _, _, con = _register(site, email="ws-c@example.test")
    _, _, sup = _register(site, "supplier", email="ws-s@example.test")
    assert _req(site, "GET", "/api/supplier/dashboard", cookie=con)[0] == 403
    assert _req(site, "GET", "/api/contractor/dashboard", cookie=sup)[0] == 403
    assert _req(site, "PATCH", "/api/supplier/profile", {"name": "X"}, cookie=con)[0] == 403
    assert _req(site, "PATCH", "/api/contractor/profile", {"name": "X"}, cookie=sup)[0] == 403
    rep = _login(site, *REP)
    assert _req(site, "GET", "/api/contractor/dashboard", cookie=rep)[0] == 403


def test_employees_keep_working_and_are_not_verification_gated(site):
    admin = _login(site, *ADMIN)
    status, body, _ = _req(site, "GET", "/api/admin/users", cookie=admin)
    assert status == 200
    rep = _login(site, *REP)
    assert _req(site, "GET", "/api/sales/dashboard", cookie=rep)[0] == 200
    me = _req(site, "GET", "/api/auth/me", cookie=rep)[1]["user"]
    assert me["account_kind"] == "employee" and me["default_landing_page"] == "sales-dashboard.html"


def test_live_rfq_and_quote_stubs_never_acknowledge_unstored_requests(site):
    _, _, cookie = _register(site, email="rfq@example.test")
    assert _req(site, "POST", "/api/contractor/rfq", {"lines": []}, cookie=cookie)[0] == 403
    _verify(site, "rfq@example.test")
    status, body, _ = _req(site, "POST", "/api/contractor/rfq", {"lines": []}, cookie=cookie)
    assert status == 501 and body["error"] == "live_rfq_not_available"


def test_self_profile_cannot_change_email_role_or_state(site):
    _, _, cookie = _register(site, email="prof@example.test")
    status, body, _ = _req(site, "PATCH", "/api/contractor/profile",
                           {"name": "New Name", "email": "x@evil.test", "roles": ["admin"],
                            "account_state": "ACTIVE", "business_name": "New Co"}, cookie=cookie)
    assert status == 200
    user = body["user"]
    assert user["display_name"] == "New Name" and user["business_name"] == "New Co"
    assert user["email"] == "prof@example.test" and user["roles"] == ["contractor"]


# ---- password reset through production flows ---------------------------------------

def test_password_reset_aliases_use_production_flow(site):
    _, _, cookie = _register(site, email="reset@example.test")
    assert _req(site, "POST", "/api/auth/forgot-password", {"email": "reset@example.test"})[0] == 202
    link = _verify_link("reset@example.test", kind=mail.PORTAL_RESET)
    token = _fragment_token(link)
    status, _, _ = _req(site, "POST", "/api/auth/reset-password", {"token": token, "password": "Brand-New-Pass-2"})
    assert status == 200
    assert _req(site, "GET", "/api/auth/me", cookie=cookie)[0] == 401  # sessions revoked
    assert _login(site, "reset@example.test", "Brand-New-Pass-2")
    assert _req(site, "POST", "/api/auth/reset-password", {"token": token, "password": "Other-Pass-3"})[0] == 400
    assert mail.sink().outbox(to="reset@example.test", kind=mail.PORTAL_PASSWORD_CHANGED)


# ---- rate limiting and client address ------------------------------------------------

def test_registration_is_throttled_per_address_and_per_email(site):
    for i in range(throttle.PORTAL_LIMITS["register_ip"]):
        assert _register(site, email=f"burst{i}@example.test")[0] == 201
    assert _register(site, email="one-more@example.test")[0] == 429
    throttle.portal.clear()
    for _ in range(throttle.PORTAL_LIMITS["register_email"]):
        _register(site, email="same@example.test")
    assert _register(site, email="same@example.test")[0] == 429


def test_shared_platform_hop_gets_platform_wide_limits_but_email_limits_hold():
    t = throttle.Throttle({"signup_ip": 10, "signup_email": 5})
    shared, visitor = "100.64.0.7", "8.8.4.4"
    assert t.limit_for("signup_ip", shared) == 10 * throttle.SHARED_ADDRESS_FACTOR
    assert t.limit_for("signup_ip", visitor) == 10
    assert t.limit_for("signup_ip", "127.0.0.1") == 10  # loopback stays per-visitor
    assert t.limit_for("signup_email", "a@example.test") == 5
    for _ in range(10):
        assert not t.hit("signup_ip", shared)
    assert not t.hit("signup_ip", shared)  # the 11th visitor is no longer locked out
    for _ in range(5):
        t.hit("signup_email", "a@example.test")
    assert t.hit("signup_email", "a@example.test")


class _H(dict):
    def get(self, key, default=None):
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


@pytest.mark.parametrize("peer,headers,source,expected", [
    ("100.64.0.7", {"X-Real-IP": "1.1.1.1"}, "railway", "1.1.1.1"),
    ("10.1.2.3", {"X-Real-IP": "9.9.9.9"}, "railway", "9.9.9.9"),
    ("fd12::1", {"X-Real-IP": "2606:4700:4700::1111"}, "railway", "2606:4700:4700::1111"),
    ("fd12::1", {"X-Real-IP": "2001:db8::1"}, "railway", "fd12::1"),          # documentation range is not global
    ("100.64.0.7", {"X-Real-IP": "1.1.1.1"}, None, "100.64.0.7"),             # mode off by default
    ("8.8.8.8", {"X-Real-IP": "9.9.9.9"}, "railway", "8.8.8.8"),             # public peer: edge bypassed
    ("127.0.0.1", {"X-Real-IP": "9.9.9.9"}, "railway", "127.0.0.1"),         # loopback is not the edge
    ("100.64.0.7", {"X-Real-IP": "10.0.0.5"}, "railway", "100.64.0.7"),       # private value refused
    ("100.64.0.7", {"X-Real-IP": "203.0.113.10"}, "railway", "100.64.0.7"),   # non-global value refused
    ("100.64.0.7", {"X-Real-IP": "1.1.1.1, 9.9.9.9"}, "railway", "100.64.0.7"),
    ("100.64.0.7", {"X-Real-IP": "not-an-ip"}, "railway", "100.64.0.7"),
    ("100.64.0.7", {"X-Forwarded-For": "8.8.8.8"}, "railway", "100.64.0.7"),  # XFF never read
    ("100.64.0.7", {"CF-Connecting-IP": "8.8.8.8"}, "railway", "100.64.0.7"),  # no secret
])
def test_railway_client_address_mode(peer, headers, source, expected, monkeypatch):
    monkeypatch.delenv(client_address.SOURCE_ENV, raising=False)
    got = client_address.resolve_client_ip(peer, _H(headers), secret="", hosted=False, source=source)
    assert got == expected


def test_cloudflare_secret_mode_still_takes_precedence(monkeypatch):
    secret = "s" * 40
    h = _H({"CF-Connecting-IP": "198.51.100.5", client_address.PROXY_SECRET: secret,
            "X-Real-IP": "1.1.1.1"})
    assert client_address.resolve_client_ip("100.64.0.7", h, secret=secret, hosted=True,
                                            source="railway") == "198.51.100.5"


def test_shared_address_classification():
    assert client_address.is_shared_address("100.64.0.7")
    assert client_address.is_shared_address("10.0.0.1")
    assert not client_address.is_shared_address("127.0.0.1")
    assert not client_address.is_shared_address("8.8.4.4")
    assert client_address.is_shared_address("fd12::1")
    # Reserved/documentation ranges are not platform hops (a trusted header may resolve them).
    assert not client_address.is_shared_address("203.0.113.10")
    assert not client_address.is_shared_address("unknown")


# ---- mail transports and readiness ---------------------------------------------------

class _FakeSMTP:
    instances: list = []

    def __init__(self, host, port, timeout=None, context=None, starttls=True):
        self.calls, self.context, self.offers_starttls = [], context, starttls
        _FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def has_extn(self, name):
        return self.offers_starttls and name.lower() == "starttls"

    def starttls(self, context=None):
        self.calls.append("starttls")
        self.context = context

    def login(self, user, password):
        self.calls.append("login")

    def send_message(self, msg):
        self.calls.append("send")


def _msg():
    return mail.Message(to="a@example.test", subject="s", text="t", kind="k")


def test_smtp_starttls_uses_a_verifying_context():
    _FakeSMTP.instances.clear()
    m = mail.SmtpMailer("smtp.example.test", 587, "user", "secret-pass", smtp_factory=_FakeSMTP)
    m.send(_msg())
    smtp = _FakeSMTP.instances[-1]
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login", "send"]
    assert smtp.context.verify_mode == ssl.CERT_REQUIRED and smtp.context.check_hostname
    assert smtp.context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_smtp_refuses_servers_without_starttls_and_never_logs_in():
    _FakeSMTP.instances.clear()
    factory = lambda *a, **k: _FakeSMTP(*a, starttls=False, **k)  # noqa: E731
    m = mail.SmtpMailer("smtp.example.test", 587, "user", "secret-pass", smtp_factory=factory)
    with pytest.raises(mail.MailSendError) as err:
        m.send(_msg())
    assert "login" not in _FakeSMTP.instances[-1].calls
    assert "secret-pass" not in str(err.value)


def test_smtp_implicit_tls_passes_a_verifying_context():
    _FakeSMTP.instances.clear()
    m = mail.SmtpMailer("smtp.example.test", 465, "user", "pw", tls_mode="implicit",
                        smtp_ssl_factory=_FakeSMTP)
    m.send(_msg())
    assert _FakeSMTP.instances[-1].context.verify_mode == ssl.CERT_REQUIRED


def test_smtp_errors_are_sanitised():
    def boom(*a, **k):
        raise OSError("connection to smtp.example.test failed with password secret-pass")
    m = mail.SmtpMailer("smtp.example.test", 587, "user", "secret-pass", smtp_factory=boom)
    with pytest.raises(mail.MailSendError) as err:
        m.send(_msg())
    assert "secret-pass" not in str(err.value) and err.value.__cause__ is None


def test_postmark_transport_verifies_tls(monkeypatch):
    seen = {}

    class _Resp:
        status = 200
        def read(self):
            return b"{}"
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None, context=None):
        seen["context"] = context
        return _Resp()

    monkeypatch.setattr(mail.urllib.request, "urlopen", fake_urlopen)
    mail._default_transport(mail.POSTMARK_API_URL, b"{}", {})
    assert seen["context"].verify_mode == ssl.CERT_REQUIRED and seen["context"].check_hostname


def test_production_never_uses_the_sink_and_validates_smtp(monkeypatch):
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.setenv(mail.ENV_PROVIDER, "sink")
    with pytest.raises(mail.MailNotConfigured):
        mail.get_mailer()
    assert "production_mail_sink" in readiness.production_problems()
    monkeypatch.setenv(mail.ENV_PROVIDER, "smtp")
    for name in (mail.ENV_SMTP_HOST, mail.ENV_SMTP_USERNAME, mail.ENV_SMTP_PASSWORD):
        monkeypatch.delenv(name, raising=False)
    problems = readiness.production_problems()
    assert "production_smtp_host_missing" in problems and "production_smtp_credentials_missing" in problems
    with pytest.raises(mail.MailNotConfigured):
        mail.get_mailer()
    monkeypatch.setenv(mail.ENV_SMTP_HOST, "smtp.postmarkapp.com")
    monkeypatch.setenv(mail.ENV_SMTP_USERNAME, "u")
    monkeypatch.setenv(mail.ENV_SMTP_PASSWORD, "p")
    monkeypatch.setenv(mail.ENV_SMTP_TLS, "none")
    assert "production_smtp_tls_mode_invalid" in readiness.production_problems()


def test_production_advisory_for_unattributed_client_addresses(monkeypatch):
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.delenv(client_address.SOURCE_ENV, raising=False)
    monkeypatch.delenv(client_address.HOSTED_ENV, raising=False)
    assert readiness.production_advisories() == ["client_ip_unattributed"]
    monkeypatch.setenv(client_address.SOURCE_ENV, "railway")
    assert readiness.production_advisories() == []


def test_mail_diagnostics_never_contain_secret_values(monkeypatch):
    monkeypatch.setenv(mail.ENV_POSTMARK_TOKEN, "pm-secret-value")
    monkeypatch.setenv(mail.ENV_SMTP_PASSWORD, "smtp-secret-value")
    blob = json.dumps(mail.diagnostics())
    assert "secret-value" not in blob


# ---- public surfaces ------------------------------------------------------------------

def test_public_preview_seo_and_health(site):
    status, body, _ = _req(site, "GET", "/api/public/preview")
    assert status == 200 and "synthetic" in json.dumps(body).lower()
    status, robots, _ = _req(site, "GET", "/robots.txt", ctype=None)
    assert status == 200 and "Disallow: /api/" in robots and "Disallow: /admin-dashboard.html" in robots
    status, sitemap, _ = _req(site, "GET", "/sitemap.xml", ctype=None)
    assert status == 200 and "register-supplier.html" in sitemap
    assert "register-contractor.html" not in sitemap and "admin-dashboard" not in sitemap
    status, health, _ = _req(site, "GET", "/api/health", ctype=None)
    assert "mail" not in health and "smtp" not in json.dumps(health).lower()


@pytest.mark.parametrize("page", ["register-contractor.html", "register-supplier.html", "verify-email.html",
                                  "reset-password.html", "contractor-welcome.html", "supplier-welcome.html",
                                  "explore.html", "auth-public.js", "auth-public.css"])
def test_onboarding_pages_are_served(site, page):
    assert _req(site, "GET", f"/{page}", ctype=None)[0] == 200
