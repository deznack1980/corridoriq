"""Contractor Pro v0.2 hardening: priority contract, provisioning, UI isolation,
billing health, operator incidents, concurrency guards. No test reaches Stripe."""

from __future__ import annotations

import inspect
import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from pipeline.auth import service as auth
from pipeline.auth.passwords import verify_password
from pipeline.auth.rbac import AuthzError
from pipeline.auth.seed import seed_auth
from pipeline.billing import contractor_accounts as ca
from pipeline.billing import entitlements, health, service, store
from pipeline.billing import webhook as wh
from pipeline.config.settings import SCHEMA_PATH
from pipeline.tests.test_billing import PRICE, SK, WHSEC, FakeGateway, _login, _req, event, sign, sub
from pipeline.tests.test_billing_contractor_pro import (CENV, PRICE_C, acct, ccfg, checkout, deliver, ents,
                                                       subscribe)


@pytest.fixture(autouse=True)
def _no_real_stripe(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tests must never construct the real Stripe client")
    monkeypatch.setattr("pipeline.billing.gateway.StripeGateway.__init__", boom)


def _factory(path):
    def f():
        c = sqlite3.connect(path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c
    return f


@pytest.fixture()
def cw(tmp_path):
    factory = _factory(tmp_path / "v02.db")
    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(c)
    now = store.now_iso()
    orgs = {"house": c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]}
    for slug, name, kind in (("desert-supply", "Desert Supply Co", "supplier"),
                             ("saguaro-plumbing", "Saguaro Plumbing LLC", "contractor"),
                             ("mesa-mechanical", "Mesa Mechanical", "contractor")):
        orgs[slug] = c.execute("INSERT INTO organizations (name, slug, is_active, account_type, created_at, updated_at) "
                               "VALUES (?, ?, 1, ?, ?, ?)", (name, slug, kind, now, now)).lastrowid
    c.commit()
    users = {}
    for key, org, role in (("house_admin", "house", "admin"), ("supplier_admin", "desert-supply", "admin"),
                           ("s_mgr", "desert-supply", "sales_manager"), ("c_owner", "saguaro-plumbing", "contractor_owner"),
                           ("c2_owner", "mesa-mechanical", "contractor_owner")):
        users[key] = auth.create_user(c, organization_id=orgs[org], email=f"{key}@example.com", password="Passw0rd!x",
                                      role_names=[role], must_change_password=False)
    c.close()
    return {"factory": factory, "orgs": orgs, "users": users}


def uctx(cw, key):
    c = cw["factory"]()
    try:
        return auth.build_user_context(c, c.execute("SELECT * FROM users WHERE id=?", (cw["users"][key],)).fetchone())
    finally:
        c.close()


def prio(cw, org, config=None):
    c = cw["factory"]()
    try:
        return entitlements.request_priority(c, cw["orgs"][org], config or ccfg())
    finally:
        c.close()


def status(cw, key, config=None):
    c = cw["factory"]()
    try:
        return service.billing_status(c, uctx(cw, key), config or ccfg())
    finally:
        c.close()


# ===================================================== priority contract =====
def test_paid_contractor_pro_is_priority(cw):
    gw = FakeGateway()
    subscribe(cw, gw)
    assert prio(cw, "saguaro-plumbing") == entitlements.PRIORITY == "priority"
    st = status(cw, "c_owner")
    assert st["request_priority"] == "priority" and st["priority_requests"] is True


def test_unpaid_and_pending_contractor_is_standard(cw):
    assert prio(cw, "saguaro-plumbing") == "standard"                         # no billing at all
    checkout(cw, "c_owner")
    assert acct(cw, "saguaro-plumbing")["billing_state"] == "checkout_pending"
    assert prio(cw, "saguaro-plumbing") == "standard"
    st = status(cw, "c_owner")
    assert st["request_priority"] == "standard" and st["priority_requests"] is False


@pytest.mark.parametrize("status_", ["incomplete", "incomplete_expired", "past_due", "unpaid", "canceled", "paused"])
def test_non_paid_states_are_standard(cw, status_):
    gw = FakeGateway()
    subscribe(cw, gw, status=status_)
    assert prio(cw, "saguaro-plumbing") == "standard" and ents(cw, "saguaro-plumbing") == frozenset()


def test_renewal_failure_to_past_due_drops_priority_no_grace(cw):
    """Policy (entitlements.PAID_ACCESS_STATES = active/trialing): past_due has no grace period."""
    from pipeline.billing.states import PAID_ACCESS_STATES, BillingState
    assert BillingState.PAST_DUE not in PAID_ACCESS_STATES
    gw = FakeGateway()
    sid = subscribe(cw, gw)
    assert prio(cw, "saguaro-plumbing") == "priority"
    gw.subscriptions[sid]["status"] = "past_due"                               # Stripe: renewal payment failed
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    deliver(cw, event("invoice.payment_failed", {"customer": cus, "billing_reason": "subscription_cycle",
                                                 "parent": {"subscription_details": {"subscription": sid}}}), gw)
    assert acct(cw, "saguaro-plumbing")["billing_state"] == "past_due"
    assert prio(cw, "saguaro-plumbing") == "standard" and ents(cw, "saguaro-plumbing") == frozenset()
    gw.subscriptions[sid]["status"] = "active"                                 # card updated, invoice paid
    deliver(cw, event("invoice.paid", {"customer": cus, "parent": {"subscription_details": {"subscription": sid}}}), gw)
    assert prio(cw, "saguaro-plumbing") == "priority"


def test_supplier_never_receives_contractor_priority(cw):
    gw = FakeGateway()
    subscribe(cw, gw, org="desert-supply", owner="s_mgr", price=PRICE)
    assert prio(cw, "desert-supply") == "standard"
    st = status(cw, "s_mgr")
    assert st["request_priority"] is None and st["priority_requests"] is False
    assert st["entitlements"] == ["supplier_intelligence"]


def test_request_priority_takes_no_request_input():
    params = list(inspect.signature(entitlements.request_priority).parameters)
    assert params == ["conn", "organization_id", "config"]


def test_contractor_on_supplier_price_gets_no_priority(cw):
    gw = FakeGateway()
    subscribe(cw, gw, price=PRICE)
    assert prio(cw, "saguaro-plumbing") == "standard" and ents(cw, "saguaro-plumbing") == frozenset()


# ======================================================== provisioning ======
GOOD = {"company_name": "Cactus Rooter LLC", "contact_name": "Ana Ruiz", "email": "Ana@CactusRooter.example",
        "business_zip": "85004"}


def create(cw, key, data, config=None):
    c = cw["factory"]()
    try:
        return ca.create_contractor_account(c, uctx(cw, key), data)
    finally:
        c.close()


def test_operator_creates_contractor_account(cw):
    res = create(cw, "house_admin", {**GOOD, "account_type": "supplier", "role": "admin", "roles": ["admin"],
                                     "plan": "founding_supply_partner", "permissions": ["admin.system"]})
    assert res["organization"]["account_type"] == "contractor" and res["owner"]["role"] == "contractor_owner"
    assert res["owner"]["email"] == "ana@cactusrooter.example" and res["owner"]["must_change_password"] is True
    c = cw["factory"]()
    org = c.execute("SELECT * FROM organizations WHERE id=?", (res["organization"]["id"],)).fetchone()
    user = c.execute("SELECT * FROM users WHERE id=?", (res["owner"]["id"],)).fetchone()
    roles = [r[0] for r in c.execute("SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=?",
                                     (user["id"],))]
    prof = c.execute("SELECT * FROM contractor_profiles WHERE organization_id=?", (org["id"],)).fetchone()
    audit = c.execute("SELECT details_json FROM security_audit_log WHERE event_type='contractor_account_created'").fetchone()
    c.close()
    assert org["account_type"] == "contractor" and org["slug"].startswith("contractor-")
    assert roles == ["contractor_owner"] and user["must_change_password"] == 1
    assert user["password_hash"] != res["temporary_password"] and verify_password(res["temporary_password"], user["password_hash"])
    assert prof["business_zip"] == "85004" and prof["roc_license"] is None
    assert res["temporary_password"] not in (audit[0] or "")
    st = status(cw, "house_admin")  # operator's own org stays non-billable
    assert st["billable"] is False


@pytest.mark.parametrize("key", ["s_mgr", "supplier_admin", "c_owner"])
def test_non_operator_cannot_create_contractor_accounts(cw, key):
    with pytest.raises(AuthzError):
        create(cw, key, GOOD)
    c = cw["factory"]()
    try:
        assert c.execute("SELECT COUNT(*) FROM organizations WHERE name='Cactus Rooter LLC'").fetchone()[0] == 0
        with pytest.raises(AuthzError):
            ca.list_contractor_accounts(c, uctx(cw, key))
    finally:
        c.close()


@pytest.mark.parametrize("data,field", [
    ({**GOOD, "email": "not-an-email"}, "email"),
    ({**GOOD, "email": ""}, "email"),
    ({k: v for k, v in GOOD.items() if k != "company_name"}, "company_name"),
    ({k: v for k, v in GOOD.items() if k != "contact_name"}, "contact_name"),
    ({k: v for k, v in GOOD.items() if k != "business_zip"}, "business_zip"),
    ({**GOOD, "business_zip": "8500"}, "business_zip"),
    ({**GOOD, "roc_license": "ROC 12/34!"}, "roc_license"),
    ({**GOOD, "phone": "123"}, "phone"),
    ({**GOOD, "company_name": "Bad\x00Name"}, "company_name"),
    ({**GOOD, "company_name": ["array"]}, "company_name"),
    ({**GOOD, "contact_name": "x" * 500}, "contact_name"),
])
def test_contractor_fields_validated(cw, data, field):
    with pytest.raises(ca.ContractorAccountError) as e:
        create(cw, "house_admin", data)
    assert field in e.value.errors


def test_roc_optional_address_instead_of_zip_and_normalisation():
    out = ca.validate_contractor_signup({**{k: v for k, v in GOOD.items() if k != "business_zip"},
                                         "business_address": "100 W Washington St, Phoenix AZ",
                                         "roc_license": "roc 123456", "phone": "(602) 555-0100"})
    assert out["business_zip"] is None and out["business_address"].startswith("100 W")
    assert out["roc_license"] == "ROC123456" and out["phone"] == "6025550100"
    assert ca.validate_contractor_signup(GOOD)["roc_license"] is None


def test_duplicate_email_leaves_no_orphan_org(cw):
    create(cw, "house_admin", GOOD)
    with pytest.raises(ca.ContractorAccountError) as e:
        create(cw, "house_admin", {**GOOD, "company_name": "Another Co"})
    assert "email" in e.value.errors
    c = cw["factory"]()
    try:
        assert c.execute("SELECT COUNT(*) FROM organizations WHERE name='Another Co'").fetchone()[0] == 0
    finally:
        c.close()


def test_provisioned_contractor_buys_only_contractor_pro(cw):
    res = create(cw, "house_admin", GOOD)
    c = cw["factory"]()
    owner = auth.build_user_context(c, c.execute("SELECT * FROM users WHERE id=?", (res["owner"]["id"],)).fetchone())
    gw = FakeGateway()
    service.start_checkout(c, owner, ccfg(), gw)
    listing = ca.list_contractor_accounts(c, uctx(cw, "house_admin"), ccfg())
    c.close()
    params = [k for n, k in gw.calls if n == "create_checkout_session"][0]["params"]
    assert params["line_items"] == [{"price": PRICE_C, "quantity": 1}]
    row = [r for r in listing if r["organization_id"] == res["organization"]["id"]][0]
    assert row["billing_state"] == "checkout_pending" and row["request_priority"] == "standard"


# ======================================================== billing health ====
def health_of(cw, config=None, now=None):
    c = cw["factory"]()
    try:
        return health.billing_health(c, config or ccfg(), now=now)
    finally:
        c.close()


def test_health_not_enabled_is_not_an_error(cw):
    h = health_of(cw, ccfg(CORRIDORIQ_BILLING_ENABLED="0"))
    assert h["status"] == health.NOT_ENABLED and h["enabled"] is False and h["configuration"]["problems"] == []


def test_health_misconfigured_is_critical(cw):
    assert health_of(cw, ccfg(STRIPE_WEBHOOK_SECRET=None))["status"] == health.CRITICAL
    assert health_of(cw, ccfg(STRIPE_SECRET_KEY="sk_live_x"))["status"] == health.CRITICAL  # live key off-production


def test_health_normal_state(cw):
    gw = FakeGateway()
    subscribe(cw, gw)
    subscribe(cw, gw, org="desert-supply", owner="s_mgr", price=PRICE)
    h = health_of(cw)
    assert h["status"] == health.HEALTHY and h["webhooks"]["last_applied_at"] and h["webhooks"]["unresolved_errors"] == 0
    assert h["subscriptions_in_paid_state"] == {"contractor": 1, "supplier": 1}
    assert h["configuration"]["plans"] == {"founding_supply_partner": {"price_configured": True},
                                           "contractor_pro": {"price_configured": True}}


def test_health_recent_failure_is_warning_then_old_is_critical(cw):
    gw = FakeGateway()
    checkout(cw, "c_owner", gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_f"] = sub("sub_f", cus, price=PRICE_C)
    gw.fail = True
    assert deliver(cw, event("customer.subscription.created", gw.subscriptions["sub_f"]), gw)[0] == 503
    h = health_of(cw)
    assert h["status"] == health.WARNING and h["webhooks"]["unresolved_errors"] == 1
    later = datetime.now(timezone.utc) + timedelta(days=4)
    assert health_of(cw, now=later)["status"] == health.CRITICAL


def test_health_mismatch_and_incidents_are_operator_visible(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw, price=PRICE)  # contractor paid the supplier price
    h = health_of(cw)
    assert h["status"] == health.WARNING and h["incidents"]["wrong_plan_payment_last_30d"] >= 1
    c = cw["factory"]()
    try:
        items = health.billing_incidents(c)
    finally:
        c.close()
    inc = [i for i in items if i["incident"] == "wrong_plan_payment"][0]
    assert inc["organization_id"] == cw["orgs"]["saguaro-plumbing"] and inc["account_type"] == "contractor"
    assert inc["expected_plan"] == "contractor_pro" and inc["observed_plan"] == "founding_supply_partner"
    assert inc["mismatch_type"] == "plan_mismatch" and inc["stripe_subscription"] == sid and inc["occurred_at"]
    assert "refund" in inc["operator_action"].lower()
    blob = json.dumps(items)
    assert acct(cw, "saguaro-plumbing")["stripe_customer_id"] not in blob


def test_health_unknown_when_tables_missing(cw):
    broken = sqlite3.connect(":memory:")
    assert health.billing_health(broken, ccfg())["status"] == health.UNKNOWN


def test_health_output_contains_no_secrets_or_ids(cw):
    gw = FakeGateway()
    subscribe(cw, gw)
    blob = json.dumps(health_of(cw))
    for secret in (SK, WHSEC, "pk_test_unit", PRICE, PRICE_C, acct(cw, "saguaro-plumbing")["stripe_customer_id"],
                   acct(cw, "saguaro-plumbing")["stripe_subscription_id"]):
        assert secret not in blob, secret


# =================================================== concurrency guards =====
def test_concurrent_billing_write_is_detected_and_retried(cw):
    gw = FakeGateway()
    checkout(cw, "c_owner", gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_c"] = sub("sub_c", cus, price=PRICE_C)
    original = gw.retrieve_subscription

    def racing(sid):  # another writer bumps the row while this delivery is mid-flight
        c = cw["factory"]()
        store.update_account(c, cw["orgs"]["saguaro-plumbing"], billing_updated_at=store.now_iso())
        c.close()
        return original(sid)

    gw.retrieve_subscription = racing
    p = event("customer.subscription.created", gw.subscriptions["sub_c"], eid="evt_race_1")
    assert deliver(cw, p, gw)[0] == 503 and ents(cw, "saguaro-plumbing") == frozenset()
    gw.retrieve_subscription = original
    assert deliver(cw, p, gw)[0] == 200 and ents(cw, "saguaro-plumbing") == frozenset({"contractor_pro"})


def test_customer_is_set_only_if_absent(cw):
    c = cw["factory"]()
    org = cw["orgs"]["saguaro-plumbing"]
    store.ensure_account(c, org)
    assert store.set_customer_if_absent(c, org, "cus_first") == "cus_first"
    assert store.set_customer_if_absent(c, org, "cus_second") == "cus_first"
    c.close()


def test_checkout_recording_never_overwrites_live_state(cw):
    gw = FakeGateway()
    subscribe(cw, gw)
    c = cw["factory"]()
    store.record_checkout_session(c, cw["orgs"]["saguaro-plumbing"], "cs_late", None)
    c.close()
    assert acct(cw, "saguaro-plumbing")["billing_state"] == "active"


def test_duplicate_subscription_protection_intact_and_visible(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_dup"] = sub("sub_dup", cus, price=PRICE_C)
    deliver(cw, event("customer.subscription.created", gw.subscriptions["sub_dup"]), gw)
    assert acct(cw, "saguaro-plumbing")["stripe_subscription_id"] == sid
    c = cw["factory"]()
    try:
        inc = [i for i in health.billing_incidents(c) if i["incident"] == "duplicate_subscription"][0]
    finally:
        c.close()
    assert inc["stripe_subscription"] == "sub_dup" and inc["expected_plan"] == "contractor_pro"


# ============================================================ HTTP layer ====
@pytest.fixture()
def site(cw, monkeypatch):
    from pipeline.api import server as server_mod

    for k, v in CENV.items():
        monkeypatch.setenv(k, v)
    gw = FakeGateway()
    monkeypatch.setattr(server_mod, "_factory", cw["factory"])
    monkeypatch.setattr(server_mod, "_billing_gateway", lambda config: gw)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"port": srv.server_address[1], "gw": gw, "cw": cw}
    srv.shutdown()


J = {"Content-Type": "application/json"}


def test_http_priority_and_plan_cannot_be_forged(site):
    cookie = _login(site["port"], "c_owner@example.com")
    forged = "?priority=true&request_priority=priority&plan=contractor_pro&entitlement=contractor_pro&account_type=contractor"
    s, _, raw = _req(site["port"], "GET", "/api/billing/status" + forged, None,
                     {"Cookie": cookie, "X-Priority": "priority", "Host": "evil.example"})
    data = json.loads(raw)
    assert s == 200 and data["request_priority"] == "standard" and data["priority_requests"] is False
    _req(site["port"], "POST", "/api/billing/checkout" + forged,
         {"priority": True, "request_priority": "priority", "entitlement": "contractor_pro", "price_id": PRICE},
         {**J, "Cookie": cookie})
    assert json.loads(_req(site["port"], "GET", "/api/billing/status", None, {"Cookie": cookie})[2])["priority_requests"] is False


def test_http_contractor_cannot_reach_supplier_intelligence(site):
    cookie = _login(site["port"], "c_owner@example.com")
    for path in ("/api/sales/companies", "/api/sales/opportunities", "/api/admin/users", "/api/manager/team",
                 "/api/products/search?q=pex", "/api/admin/billing/incidents", "/api/admin/contractor-accounts",
                 "/api/admin/billing/health"):
        assert _req(site["port"], "GET", path, None, {"Cookie": cookie})[0] == 403, path


def test_http_contractor_provisioning_authorization(site):
    body = {**GOOD, "email": "new.owner@example.com"}
    assert _req(site["port"], "POST", "/api/admin/contractor-accounts", body, J)[0] == 401
    for who in ("s_mgr@example.com", "supplier_admin@example.com", "c_owner@example.com"):
        cookie = _login(site["port"], who)
        assert _req(site["port"], "POST", "/api/admin/contractor-accounts", body, {**J, "Cookie": cookie})[0] == 403, who
    admin = _login(site["port"], "house_admin@example.com")
    assert _req(site["port"], "POST", "/api/admin/contractor-accounts", b"company_name=x",
                {"Content-Type": "application/x-www-form-urlencoded", "Cookie": admin})[0] == 415
    s, _, raw = _req(site["port"], "POST", "/api/admin/contractor-accounts", {**body, "email": "bad"}, {**J, "Cookie": admin})
    assert s == 400 and "email" in json.loads(raw)["fields"]
    s, _, raw = _req(site["port"], "POST", "/api/admin/contractor-accounts", body, {**J, "Cookie": admin})
    res = json.loads(raw)
    assert s == 201 and res["organization"]["account_type"] == "contractor" and res["temporary_password"]
    s, _, raw = _req(site["port"], "GET", "/api/admin/contractor-accounts", None, {"Cookie": admin})
    assert s == 200 and any(a["organization_id"] == res["organization"]["id"] for a in json.loads(raw)["items"])


def test_http_admin_health_and_incidents(site):
    admin = _login(site["port"], "house_admin@example.com")
    s, _, raw = _req(site["port"], "GET", "/api/admin/billing/health", None, {"Cookie": admin})
    assert s == 200 and json.loads(raw)["status"] in (health.HEALTHY, health.WARNING)
    assert SK not in raw.decode() and WHSEC not in raw.decode()
    assert _req(site["port"], "GET", "/api/admin/billing/incidents", None, {"Cookie": admin})[0] == 200
    supplier_admin = _login(site["port"], "supplier_admin@example.com")
    assert _req(site["port"], "GET", "/api/admin/billing/incidents", None, {"Cookie": supplier_admin})[0] == 403


def test_http_billing_disabled_hides_provisioning_but_health_reports(site, monkeypatch):
    monkeypatch.setenv("CORRIDORIQ_BILLING_ENABLED", "0")
    admin = _login(site["port"], "house_admin@example.com")
    assert _req(site["port"], "POST", "/api/admin/contractor-accounts", GOOD, {**J, "Cookie": admin})[0] == 404
    assert _req(site["port"], "GET", "/api/admin/contractor-accounts", None, {"Cookie": admin})[0] == 404
    for page in ("/contractor-accounts.html", "/contractor-accounts.js", "/billing.html"):
        assert _req(site["port"], "GET", page)[0] == 404, page
    s, _, raw = _req(site["port"], "GET", "/api/admin/billing/health", None, {"Cookie": admin})
    assert s == 200 and json.loads(raw)["status"] == health.NOT_ENABLED


def test_http_cross_tenant_status_isolation(site):
    gw = site["gw"]
    subscribe(site["cw"], gw)  # Saguaro (c_owner) is paid; Mesa (c2_owner) is not
    a = json.loads(_req(site["port"], "GET", "/api/billing/status", None,
                        {"Cookie": _login(site["port"], "c_owner@example.com")})[2])
    b = json.loads(_req(site["port"], "GET", f"/api/billing/status?organization_id={site['cw']['orgs']['saguaro-plumbing']}",
                        None, {"Cookie": _login(site["port"], "c2_owner@example.com")})[2])
    assert a["priority_requests"] is True and b["priority_requests"] is False and b["state"] == "none"


# ====================================================== browser: UI shell ===
@pytest.fixture()
def browser():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("Chromium not installed for Playwright")
        yield b
        b.close()


def _ui_login(page, port, email):
    page.goto(f"http://127.0.0.1:{port}/login.html", wait_until="networkidle")
    page.fill("#email", email)
    page.fill("#password", "Passw0rd!x")
    page.click("#loginBtn")
    page.wait_for_url(lambda u: "login.html" not in u, timeout=15000)
    page.wait_for_load_state("networkidle")


def test_ui_contractor_shell_hides_supplier_controls_and_supplier_unchanged(site, browser):
    port = site["port"]
    c = browser.new_context().new_page()
    errors = []
    c.on("pageerror", lambda e: errors.append(str(e)))
    _ui_login(c, port, "c_owner@example.com")
    assert c.url.endswith("/billing.html")
    c.wait_for_selector("#billing .card", timeout=15000)
    nav = [t.strip() for t in c.locator(".sidebar .nav .nav-item").all_inner_texts()]
    assert len(nav) == 1 and "Contractor Pro" in nav[0], nav
    assert c.locator("#ciqSearch").count() == 0 and c.locator("#ciqTasksBtn").count() == 0
    assert "contractor account" in c.locator(".portal-label").inner_text().lower()
    assert "Priority Requests" in c.inner_text("#billing") and "on their own schedule" in c.inner_text("#billing")
    assert errors == [], errors
    s = browser.new_context().new_page()
    _ui_login(s, port, "s_mgr@example.com")
    s.goto(f"http://127.0.0.1:{port}/billing.html", wait_until="networkidle")
    s.wait_for_selector("#billing .card", timeout=15000)
    assert s.locator("#ciqSearch").count() == 1 and s.locator("#ciqTasksBtn").count() == 1
    labels = " ".join(s.locator(".sidebar .nav .nav-item").all_inner_texts())
    assert "Companies" in labels or "Assignments" in labels
    assert "Priority Requests" not in s.inner_text("#billing")
