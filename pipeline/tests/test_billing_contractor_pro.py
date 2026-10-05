"""Contractor Pro ($99/month) on the existing billing architecture.

Covers plan configuration, server-side eligibility by organization account
type, cross-plan isolation (a contractor can never buy or receive the supplier
plan and vice versa), Checkout/Portal/webhook behaviour for the contractor
plan, and the priority-request entitlement helper. No test reaches Stripe.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.auth import service as auth
from pipeline.auth.rbac import AuthzError, ROLES, default_landing_page, load_user_permissions
from pipeline.auth.seed import seed_auth
from pipeline.billing import entitlements, service, store
from pipeline.billing import webhook as wh
from pipeline.billing.__main__ import verify_price
from pipeline.billing.config import load_config
from pipeline.billing.plans import CONTRACTOR_PRO, FOUNDING_SUPPLY_PARTNER, plan_for_account_type
from pipeline.config.settings import SCHEMA_PATH
from pipeline.db.database import migrate_schema
from pipeline.tests.test_billing import ENV, PRICE, FakeGateway, _login, _req, event, sign, sub

PRICE_C = "price_TESTcontractorpro"
CENV = {**ENV, "STRIPE_CONTRACTOR_PRO_PRICE_ID": PRICE_C,
        "STRIPE_CONTRACTOR_PRO_PRICE_LOOKUP_KEY": "corridoriq_contractor_pro_monthly_test"}
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _no_real_stripe(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tests must never construct the real Stripe client")
    monkeypatch.setattr("pipeline.billing.gateway.StripeGateway.__init__", boom)


def ccfg(**over):
    env = {**CENV, **over}
    return load_config({k: v for k, v in env.items() if v is not None})


def _factory(path):
    def f():
        c = sqlite3.connect(path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c
    return f


@pytest.fixture()
def cw(tmp_path):
    """Supplier + two contractor organizations and their users."""
    factory = _factory(tmp_path / "cpro.db")
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
    for key, org, role in (("s_mgr", "desert-supply", "sales_manager"),
                           ("c_owner", "saguaro-plumbing", "contractor_owner"),
                           ("c_viewer", "saguaro-plumbing", "read_only"),
                           ("c2_owner", "mesa-mechanical", "contractor_owner")):
        users[key] = auth.create_user(c, organization_id=orgs[org], email=f"{key}@example.com", password="Passw0rd!x",
                                      role_names=[role], must_change_password=False)
    c.close()
    return {"factory": factory, "orgs": orgs, "users": users}


def ctx(cw, key):
    c = cw["factory"]()
    try:
        return auth.build_user_context(c, c.execute("SELECT * FROM users WHERE id=?", (cw["users"][key],)).fetchone())
    finally:
        c.close()


def acct(cw, org):
    c = cw["factory"]()
    try:
        return store.get_account(c, cw["orgs"][org])
    finally:
        c.close()


def checkout(cw, key, gw=None, config=None):
    gw = gw or FakeGateway()
    c = cw["factory"]()
    try:
        return service.start_checkout(c, ctx(cw, key), config or ccfg(), gw), gw
    finally:
        c.close()


def portal(cw, key, gw, config=None):
    c = cw["factory"]()
    try:
        return service.create_portal_session(c, ctx(cw, key), config or ccfg(), gw)
    finally:
        c.close()


def deliver(cw, payload, gw, config=None, header=None):
    c = cw["factory"]()
    try:
        return wh.handle_webhook(c, payload, sign(payload) if header is None else header, config or ccfg(), gw)
    finally:
        c.close()


def ents(cw, org, config=None):
    c = cw["factory"]()
    try:
        return entitlements.entitlements_for(c, cw["orgs"][org], config or ccfg())
    finally:
        c.close()


def outcome(cw, event_id):
    c = cw["factory"]()
    try:
        r = c.execute("SELECT outcome FROM billing_stripe_events WHERE event_id=?", (event_id,)).fetchone()
        return r["outcome"] if r else None
    finally:
        c.close()


def audits(cw, org):
    c = cw["factory"]()
    try:
        return [r["event_type"] for r in c.execute(
            "SELECT event_type FROM security_audit_log WHERE organization_id=? AND event_type LIKE 'billing_%' ORDER BY id",
            (cw["orgs"][org],))]
    finally:
        c.close()


def subscribe(cw, gw, org="saguaro-plumbing", owner="c_owner", price=PRICE_C, status="active", **kw):
    checkout(cw, owner, gw)
    cus = acct(cw, org)["stripe_customer_id"]
    sid = f"sub_{org}"
    gw.subscriptions[sid] = sub(sid, cus, status=status, price=price, **kw)
    payload = event("checkout.session.completed", {
        "mode": "subscription", "customer": cus, "subscription": sid,
        "client_reference_id": str(cw["orgs"][org]),
        "metadata": {"corridoriq_tenant_id": str(cw["orgs"][org])}})
    assert deliver(cw, payload, gw)[0] == 200
    return sid


# ------------------------------------------------------------- 1-4 config ---
def test_contractor_pro_configured_alongside_founding_partner():
    c = ccfg()
    assert c.problems() == [] and c.plan_problems(CONTRACTOR_PRO) == [] and c.plan_problems(FOUNDING_SUPPLY_PARTNER) == []
    assert c.price_id_for(CONTRACTOR_PRO) == PRICE_C and c.price_id_for(FOUNDING_SUPPLY_PARTNER) == PRICE
    assert c.plan_for_price(PRICE_C) is CONTRACTOR_PRO and c.plan_for_price(PRICE) is FOUNDING_SUPPLY_PARTNER
    assert c.plan_for_price("price_unknown") is None
    assert CONTRACTOR_PRO.unit_amount == 9900 and CONTRACTOR_PRO.interval == "month"
    assert FOUNDING_SUPPLY_PARTNER.unit_amount == 75000 and FOUNDING_SUPPLY_PARTNER.display_price == "$750/month"


@pytest.mark.parametrize("over,needle", [
    ({"STRIPE_CONTRACTOR_PRO_PRICE_ID": PRICE}, "must differ"),
    ({"STRIPE_CONTRACTOR_PRO_PRICE_ID": "prod_contractor"}, "not a Stripe price ID"),
])
def test_contractor_price_misconfiguration_fails_closed(over, needle):
    assert any(needle in p for p in ccfg(**over).problems())


def test_contractor_price_optional_for_supplier_but_required_for_contractor_checkout(cw):
    c = ccfg(STRIPE_CONTRACTOR_PRO_PRICE_ID=None)
    assert c.problems() == [] and any("STRIPE_CONTRACTOR_PRO_PRICE_ID" in p for p in c.plan_problems(CONTRACTOR_PRO))
    with pytest.raises(service.BillingError) as e:
        checkout(cw, "c_owner", config=c)
    assert e.value.status == 503
    assert checkout(cw, "s_mgr", config=c)[0]["url"].startswith("https://checkout.stripe.com/")


def _price(**over):
    base = {"livemode": False, "active": True, "type": "recurring", "currency": "usd", "unit_amount": 9900,
            "recurring": {"interval": "month", "interval_count": 1},
            "lookup_key": "corridoriq_contractor_pro_monthly_test", "product": "prod_test_cpro"}
    base.update(over)
    return base


class PriceGW(FakeGateway):
    def __init__(self, prices):
        super().__init__()
        self.prices = prices

    def retrieve_price(self, price_id):
        self.calls.append(("retrieve_price", {"price_id": price_id}))
        return {"id": price_id, **self.prices[price_id]}


@pytest.mark.parametrize("over,ok", [
    ({}, True),
    ({"unit_amount": 75000}, False),                                     # $99 amount validation
    ({"unit_amount": 9800}, False),
    ({"recurring": {"interval": "year", "interval_count": 1}}, False),   # monthly interval validation
    ({"recurring": {"interval": "month", "interval_count": 3}}, False),
    ({"livemode": True}, False),                                         # test/live mode validation
    ({"currency": "eur"}, False),
    ({"active": False}, False),
    ({"lookup_key": "something_else"}, False),
])
def test_verify_price_checks_contractor_amount_interval_and_mode(over, ok, capsys):
    gw = PriceGW({PRICE_C: _price(**over)})
    assert (verify_price(ccfg(), allow_live=False, gateway=gw, plan_key="contractor_pro") == 0) is ok
    assert "sk_test" not in capsys.readouterr().out


def test_verify_price_checks_both_plans_by_default():
    gw = PriceGW({PRICE: _price(unit_amount=75000, lookup_key="corridoriq_founding_supply_partner_monthly"),
                  PRICE_C: _price()})
    assert verify_price(ccfg(), allow_live=False, gateway=gw) == 0
    assert {k["price_id"] for n, k in gw.calls if n == "retrieve_price"} == {PRICE, PRICE_C}


# ------------------------------------------------------- 5 eligibility -----
def test_eligibility_comes_from_organization_account_type(cw):
    assert plan_for_account_type("contractor") is CONTRACTOR_PRO
    assert plan_for_account_type("supplier") is FOUNDING_SUPPLY_PARTNER
    assert plan_for_account_type("CONTRACTOR") is None and plan_for_account_type(None) is None
    c = cw["factory"]()
    org = lambda slug: c.execute("SELECT * FROM organizations WHERE slug=?", (slug,)).fetchone()  # noqa: E731
    assert service.eligible_plan(org("saguaro-plumbing")) is CONTRACTOR_PRO
    assert service.eligible_plan(org("desert-supply")) is FOUNDING_SUPPLY_PARTNER
    assert service.eligible_plan(org("corridoriq")) is None
    c.execute("UPDATE organizations SET account_type='partner' WHERE slug='mesa-mechanical'")
    assert service.eligible_plan(org("mesa-mechanical")) is None
    c.close()


def test_unknown_account_type_cannot_check_out(cw):
    c = cw["factory"]()
    c.execute("UPDATE organizations SET account_type='partner' WHERE slug='mesa-mechanical'")
    c.commit()
    c.close()
    with pytest.raises(service.BillingError) as e:
        checkout(cw, "c2_owner")
    assert e.value.status == 403


def test_contractor_owner_role_is_billing_only_and_lands_on_billing(cw):
    assert ROLES["contractor_owner"]["permissions"] == {"billing.manage"}
    assert default_landing_page(["contractor_owner"], {"billing.manage"}) == "billing.html"
    c = cw["factory"]()
    try:
        assert load_user_permissions(c, cw["users"]["c_owner"]) == {"billing.manage"}
    finally:
        c.close()
    with pytest.raises(AuthzError):
        from pipeline.crm import service as crm
        c = cw["factory"]()
        try:
            crm.list_my_companies(c, ctx(cw, "c_owner"))
        finally:
            c.close()


# ------------------------------------------------------- 8, 15 checkout ----
def test_contractor_checkout_uses_contractor_price_and_metadata(cw):
    result, gw = checkout(cw, "c_owner")
    assert result["url"].startswith("https://checkout.stripe.com/")
    cust = [k for n, k in gw.calls if n == "create_customer"][0]
    params = [k for n, k in gw.calls if n == "create_checkout_session"][0]["params"]
    org = str(cw["orgs"]["saguaro-plumbing"])
    meta = {"corridoriq_tenant_id": org, "corridoriq_account_type": "contractor", "corridoriq_plan": "contractor_pro"}
    assert cust["metadata"] == meta and cust["name"] == "Saguaro Plumbing LLC"
    assert params["line_items"] == [{"price": PRICE_C, "quantity": 1}]
    assert params["metadata"] == meta and params["subscription_data"]["metadata"] == meta
    assert params["client_reference_id"] == org and params["mode"] == "subscription"
    assert PRICE not in json.dumps(params)


def test_contractor_checkout_grants_nothing(cw):
    checkout(cw, "c_owner")
    a = acct(cw, "saguaro-plumbing")
    assert a["billing_state"] == "checkout_pending" and ents(cw, "saguaro-plumbing") == frozenset()
    c = cw["factory"]()
    try:
        assert entitlements.contractor_has_pro(c, cw["orgs"]["saguaro-plumbing"], ccfg()) is False
    finally:
        c.close()


def test_supplier_checkout_unchanged_by_contractor_plan(cw):
    _, gw = checkout(cw, "s_mgr")
    params = [k for n, k in gw.calls if n == "create_checkout_session"][0]["params"]
    assert params["line_items"] == [{"price": PRICE, "quantity": 1}]
    assert params["metadata"]["corridoriq_account_type"] == "supplier"
    assert params["metadata"]["corridoriq_plan"] == "founding_supply_partner"


def test_open_session_for_other_price_is_not_resumed(cw):
    """A session left open under a different price config is replaced, not resumed."""
    _, gw = checkout(cw, "c_owner")
    sid = acct(cw, "saguaro-plumbing")["checkout_session_id"]
    gw.sessions[sid]["line_items"]["data"][0]["price"]["id"] = PRICE
    checkout(cw, "c_owner", gw)
    assert [n for n, _ in gw.calls].count("create_checkout_session") == 2


# ------------------------------------------------ 16-23 entitlement flow ----
def test_verified_contractor_pro_grants_only_contractor_pro(cw):
    gw = FakeGateway()
    subscribe(cw, gw)
    assert ents(cw, "saguaro-plumbing") == frozenset({"contractor_pro"})
    c = cw["factory"]()
    try:
        assert entitlements.contractor_has_pro(c, cw["orgs"]["saguaro-plumbing"], ccfg())
        assert not entitlements.supplier_has_paid_access(c, cw["orgs"]["saguaro-plumbing"], ccfg())
        assert entitlements.request_priority(c, cw["orgs"]["saguaro-plumbing"], ccfg()) == "priority"
        assert entitlements.request_priority(c, cw["orgs"]["mesa-mechanical"], ccfg()) == "standard"
    finally:
        c.close()
    assert "billing_subscription_activated" in audits(cw, "saguaro-plumbing")


def test_supplier_plan_never_grants_contractor_pro(cw):
    gw = FakeGateway()
    subscribe(cw, gw, org="desert-supply", owner="s_mgr", price=PRICE)
    assert ents(cw, "desert-supply") == frozenset({"supplier_intelligence"})
    c = cw["factory"]()
    try:
        assert not entitlements.contractor_has_pro(c, cw["orgs"]["desert-supply"], ccfg())
        assert entitlements.request_priority(c, cw["orgs"]["desert-supply"], ccfg()) == "standard"
    finally:
        c.close()


@pytest.mark.parametrize("status", ["incomplete", "past_due", "unpaid", "incomplete_expired"])
def test_failed_or_pending_contractor_payment_grants_nothing(cw, status):
    gw = FakeGateway()
    subscribe(cw, gw, status=status)  # incomplete = first payment failed / 3DS pending
    assert ents(cw, "saguaro-plumbing") == frozenset()


def test_3ds_pending_then_completed(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw, status="incomplete")
    assert ents(cw, "saguaro-plumbing") == frozenset()
    gw.subscriptions[sid]["status"] = "active"
    deliver(cw, event("invoice.paid", {"customer": acct(cw, "saguaro-plumbing")["stripe_customer_id"],
                                       "parent": {"subscription_details": {"subscription": sid}}}), gw)
    assert ents(cw, "saguaro-plumbing") == frozenset({"contractor_pro"})


def test_payment_failure_after_activation_removes_access(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw)
    gw.subscriptions[sid]["status"] = "past_due"
    deliver(cw, event("invoice.payment_failed", {"customer": acct(cw, "saguaro-plumbing")["stripe_customer_id"],
                                                 "parent": {"subscription_details": {"subscription": sid}}}), gw)
    assert ents(cw, "saguaro-plumbing") == frozenset() and "billing_payment_failed" in audits(cw, "saguaro-plumbing")


def test_contractor_cancellation_and_cancel_at_period_end(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw)
    gw.subscriptions[sid]["cancel_at_period_end"] = True
    deliver(cw, event("customer.subscription.updated", gw.subscriptions[sid]), gw)
    assert acct(cw, "saguaro-plumbing")["cancel_at_period_end"] == 1
    assert ents(cw, "saguaro-plumbing") == frozenset({"contractor_pro"})       # until the period ends
    c = cw["factory"]()
    later = datetime.now(timezone.utc) + timedelta(days=40)
    assert not entitlements.contractor_has_pro(c, cw["orgs"]["saguaro-plumbing"], ccfg(), now=later)
    c.close()
    gw.subscriptions[sid]["status"] = "canceled"
    deliver(cw, event("customer.subscription.deleted", gw.subscriptions[sid]), gw)
    assert acct(cw, "saguaro-plumbing")["billing_state"] == "canceled" and ents(cw, "saguaro-plumbing") == frozenset()


# ---------------------------------------------------------- 24 portal -------
def test_contractor_portal_isolation(cw):
    gw = FakeGateway()
    with pytest.raises(service.BillingError) as e:
        portal(cw, "c_owner", gw)
    assert e.value.status == 409                                             # Stripe Customer required
    checkout(cw, "c_owner", gw)
    checkout(cw, "s_mgr", gw)
    c_cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    s_cus = acct(cw, "desert-supply")["stripe_customer_id"]
    portal(cw, "c_owner", gw)
    portal(cw, "s_mgr", gw)
    used = [k["customer"] for n, k in gw.calls if n == "create_portal_session"]
    assert used == [c_cus, s_cus] and c_cus != s_cus
    with pytest.raises(service.BillingError):
        portal(cw, "c2_owner", gw)                                           # other contractor: no customer
    with pytest.raises(AuthzError):
        portal(cw, "c_viewer", gw)                                           # no billing.manage


# --------------------------------------------- 25-33 webhook / duplicates ---
def test_duplicate_contractor_subscription_protection(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw)
    with pytest.raises(service.BillingError) as e:
        checkout(cw, "c_owner", gw)
    assert e.value.status == 409
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_second"] = sub("sub_second", cus, price=PRICE_C)
    p = event("customer.subscription.created", gw.subscriptions["sub_second"])
    assert deliver(cw, p, gw)[0] == 200 and outcome(cw, json.loads(p)["id"]) == "ignored:duplicate_subscription"
    assert acct(cw, "saguaro-plumbing")["stripe_subscription_id"] == sid
    assert "billing_duplicate_subscription" in audits(cw, "saguaro-plumbing")


def test_duplicate_contractor_event_is_idempotent(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw)
    gw.subscriptions[sid]["status"] = "canceled"
    p = event("customer.subscription.deleted", gw.subscriptions[sid], eid="evt_cpro_dup")
    assert deliver(cw, p, gw) == (200, {"received": True})
    n = len(gw.calls)
    assert deliver(cw, p, gw) == (200, {"received": True, "duplicate": True}) and len(gw.calls) == n
    assert audits(cw, "saguaro-plumbing").count("billing_subscription_canceled") == 1


def test_contractor_subscription_on_supplier_price_fails_closed(cw):
    """Wrong-account plan: a contractor paying the $750 supplier price gets nothing."""
    gw = FakeGateway()
    p_id = subscribe(cw, gw, price=PRICE)
    a = acct(cw, "saguaro-plumbing")
    assert a["stripe_subscription_id"] == p_id and a["stripe_price_id"] == PRICE
    assert ents(cw, "saguaro-plumbing") == frozenset()                       # neither contractor_pro nor supplier
    assert "billing_price_mismatch" in audits(cw, "saguaro-plumbing")
    c = cw["factory"]()
    reason = c.execute("SELECT details_json FROM security_audit_log WHERE event_type='billing_price_mismatch'").fetchone()[0]
    c.close()
    assert "plan_mismatch" in reason


def test_supplier_subscription_on_contractor_price_fails_closed(cw):
    gw = FakeGateway()
    subscribe(cw, gw, org="desert-supply", owner="s_mgr", price=PRICE_C)
    assert ents(cw, "desert-supply") == frozenset()


def test_unknown_price_fails_closed(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw, price="price_SOMETHING_ELSE")
    c = cw["factory"]()
    out = [r[0] for r in c.execute("SELECT outcome FROM billing_stripe_events")]
    c.close()
    assert "applied:unknown_price" in out and ents(cw, "saguaro-plumbing") == frozenset()
    assert acct(cw, "saguaro-plumbing")["stripe_subscription_id"] == sid


def test_subscription_with_extra_item_fails_closed(cw):
    gw = FakeGateway()
    sid = subscribe(cw, gw)
    gw.subscriptions[sid]["items"]["data"].append({"price": {"id": PRICE}, "current_period_end": 0})
    deliver(cw, event("customer.subscription.updated", gw.subscriptions[sid]), gw)
    assert ents(cw, "saguaro-plumbing") == frozenset()


def test_wrong_mode_fails_closed(cw):
    gw = FakeGateway()
    checkout(cw, "c_owner", gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_live"] = sub("sub_live", cus, price=PRICE_C, livemode=True)
    assert deliver(cw, event("customer.subscription.created", gw.subscriptions["sub_live"], livemode=True), gw)[0] == 400
    p = event("customer.subscription.created", {"id": "sub_live", "customer": cus})
    deliver(cw, p, gw)
    assert outcome(cw, json.loads(p)["id"]) == "rejected:mode_mismatch" and ents(cw, "saguaro-plumbing") == frozenset()


def test_customer_mismatch_fails_closed(cw):
    gw = FakeGateway()
    checkout(cw, "c_owner", gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_other"] = sub("sub_other", "cus_someone_else", price=PRICE_C)
    p = event("customer.subscription.updated", {"id": "sub_other", "customer": cus})
    deliver(cw, p, gw)
    assert outcome(cw, json.loads(p)["id"]) == "rejected:customer_mismatch"


@pytest.mark.parametrize("meta", [
    {"corridoriq_account_type": "supplier"},
    {"corridoriq_plan": "founding_supply_partner"},
])
def test_checkout_metadata_mismatch_fails_closed(cw, meta):
    gw = FakeGateway()
    checkout(cw, "c_owner", gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_m"] = sub("sub_m", cus, price=PRICE_C)
    p = event("checkout.session.completed", {"mode": "subscription", "customer": cus, "subscription": "sub_m",
                                             "client_reference_id": str(cw["orgs"]["saguaro-plumbing"]), "metadata": meta})
    deliver(cw, p, gw)
    assert outcome(cw, json.loads(p)["id"]) == "rejected:metadata_mismatch" and ents(cw, "saguaro-plumbing") == frozenset()


@pytest.mark.parametrize("meta", [
    {"corridoriq_tenant_id": "999"},
    {"corridoriq_account_type": "supplier"},
    {"corridoriq_plan": "founding_supply_partner"},
])
def test_subscription_metadata_mismatch_fails_closed(cw, meta):
    gw = FakeGateway()
    checkout(cw, "c_owner", gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_meta"] = {**sub("sub_meta", cus, price=PRICE_C), "metadata": meta}
    p = event("customer.subscription.created", gw.subscriptions["sub_meta"])
    deliver(cw, p, gw)
    assert outcome(cw, json.loads(p)["id"]) == "rejected:metadata_mismatch"
    assert acct(cw, "saguaro-plumbing")["stripe_subscription_id"] is None


def test_tenant_claim_mismatch_fails_closed(cw):
    gw = FakeGateway()
    checkout(cw, "c_owner", gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_t"] = sub("sub_t", cus, price=PRICE_C)
    p = event("checkout.session.completed", {"mode": "subscription", "customer": cus, "subscription": "sub_t",
                                             "client_reference_id": str(cw["orgs"]["mesa-mechanical"])})
    deliver(cw, p, gw)
    assert outcome(cw, json.loads(p)["id"]) == "rejected:tenant_mismatch"


@pytest.mark.parametrize("payload", [b"{bad", b"[]", b'{"id":"evt_x","type":"invoice.paid"}'])
def test_malformed_webhook_rejected(cw, payload):
    assert deliver(cw, payload, FakeGateway())[0] == 400


def test_stripe_outage_fails_closed_then_retry_applies(cw):
    gw = FakeGateway()
    checkout(cw, "c_owner", gw)
    cus = acct(cw, "saguaro-plumbing")["stripe_customer_id"]
    gw.subscriptions["sub_o"] = sub("sub_o", cus, price=PRICE_C)
    p = event("customer.subscription.created", gw.subscriptions["sub_o"], eid="evt_cpro_outage")
    gw.fail = True
    assert deliver(cw, p, gw)[0] == 503 and ents(cw, "saguaro-plumbing") == frozenset()
    gw.fail = False
    assert deliver(cw, p, gw)[0] == 200 and ents(cw, "saguaro-plumbing") == frozenset({"contractor_pro"})


# --------------------------------------------------- 35 cross-plan ----------
def test_cross_plan_isolation_with_both_plans_active(cw):
    gw = FakeGateway()
    subscribe(cw, gw)
    subscribe(cw, gw, org="desert-supply", owner="s_mgr", price=PRICE)
    assert ents(cw, "saguaro-plumbing") == frozenset({"contractor_pro"})
    assert ents(cw, "desert-supply") == frozenset({"supplier_intelligence"})
    assert ents(cw, "mesa-mechanical") == frozenset()
    # Re-typing an organization after purchase never transfers or widens access.
    c = cw["factory"]()
    c.execute("UPDATE organizations SET account_type='supplier' WHERE slug='saguaro-plumbing'")
    c.commit()
    c.close()
    assert ents(cw, "saguaro-plumbing") == frozenset()


def test_schema_migration_adds_account_type_to_existing_organizations(tmp_path):
    c = sqlite3.connect(tmp_path / "old.db")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE organizations (id INTEGER PRIMARY KEY, name TEXT, slug TEXT, is_active INTEGER, "
              "created_at TEXT, updated_at TEXT)")
    c.execute("INSERT INTO organizations VALUES (1, 'Old Supplier', 'old', 1, 'x', 'x')")
    migrate_schema(c)
    assert c.execute("SELECT account_type FROM organizations WHERE id=1").fetchone()[0] == "supplier"
    migrate_schema(c)  # idempotent
    c.close()


def test_contractor_copy_makes_no_unsupported_claims(cw):
    text = " ".join([CONTRACTOR_PRO.tagline, (REPO / "billing.js").read_text(encoding="utf-8")])
    forbidden = (r"guarantee", r"\bSLA\b", r"response time", r"live (?:supplier )?(?:inventory|pricing|stock)",
                 r"real[- ]time", r"photo", r"automatic compatib", r"instant quote", r"savings", r"\bAI\b")
    for pat in forbidden:
        assert not re.search(pat, text, re.I), pat
    assert "$99/month" == CONTRACTOR_PRO.display_price


# ---------------------------------------------------- HTTP manipulation -----
@pytest.fixture()
def csite(cw, monkeypatch):
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


EVIL = {"plan": "founding_supply_partner", "plan_key": "founding_supply_partner", "price": PRICE, "price_id": PRICE,
        "account_type": "supplier", "audience": "supplier", "quantity": 25, "amount": 1, "currency": "eur",
        "customer": "cus_victim", "organization_id": 1, "tenant_id": 1, "entitlement": "supplier_intelligence",
        "success_url": "https://evil.example/"}
H_EVIL = {"Content-Type": "application/json", "Host": "evil.example", "X-Forwarded-Host": "evil.example"}


def _sessions(gw):
    return [k["params"] for n, k in gw.calls if n == "create_checkout_session"]


def test_http_unauthenticated_contractor_checkout_rejected(csite):
    assert _req(csite["port"], "POST", "/api/billing/checkout", {}, {"Content-Type": "application/json"})[0] == 401


def test_http_contractor_without_billing_manage_rejected(csite):
    cookie = _login(csite["port"], "c_viewer@example.com")
    assert _req(csite["port"], "POST", "/api/billing/checkout", {}, {**H_EVIL, "Cookie": cookie})[0] == 403


def test_http_contractor_cannot_buy_supplier_plan(csite):
    cookie = _login(csite["port"], "c_owner@example.com")
    s, _, raw = _req(csite["port"], "POST", "/api/billing/checkout?plan=founding_supply_partner&price=" + PRICE,
                     {**EVIL}, {**H_EVIL, "Cookie": cookie})
    assert s == 200 and json.loads(raw)["url"].startswith("https://checkout.stripe.com/")
    (p,) = _sessions(csite["gw"])
    assert p["line_items"] == [{"price": PRICE_C, "quantity": 1}]
    assert p["metadata"]["corridoriq_plan"] == "contractor_pro" and p["metadata"]["corridoriq_account_type"] == "contractor"
    assert p["client_reference_id"] == str(csite["cw"]["orgs"]["saguaro-plumbing"])
    assert p["customer"] != "cus_victim" and "evil" not in json.dumps(p) and PRICE not in json.dumps(p)


def test_http_supplier_cannot_buy_contractor_pro(csite):
    cookie = _login(csite["port"], "s_mgr@example.com")
    evil = {**EVIL, "plan": "contractor_pro", "plan_key": "contractor_pro", "price": PRICE_C, "price_id": PRICE_C,
            "account_type": "contractor", "audience": "contractor", "entitlement": "contractor_pro"}
    s, _, _ = _req(csite["port"], "POST", "/api/billing/checkout?plan=contractor_pro&account_type=contractor",
                   evil, {**H_EVIL, "Cookie": cookie})
    assert s == 200
    (p,) = _sessions(csite["gw"])
    assert p["line_items"] == [{"price": PRICE, "quantity": 1}] and PRICE_C not in json.dumps(p)
    assert p["metadata"]["corridoriq_plan"] == "founding_supply_partner"


def test_http_contractor_status_shows_contractor_plan_only(csite):
    cookie = _login(csite["port"], "c_owner@example.com")
    s, _, raw = _req(csite["port"], "GET", "/api/billing/status?account_type=supplier", None,
                     {"Cookie": cookie, "Host": "evil.example"})
    data = json.loads(raw)
    assert s == 200 and data["plan"]["key"] == "contractor_pro" and data["plan"]["display_price"] == "$99/month"
    assert data["account_type"] == "contractor" and data["paid_access"] is False and data["entitlements"] == []
    assert "sk_test" not in raw.decode()


def test_http_cross_org_portal_impossible(csite):
    gw = csite["gw"]
    cookie_c = _login(csite["port"], "c_owner@example.com")
    cookie_s = _login(csite["port"], "s_mgr@example.com")
    _req(csite["port"], "POST", "/api/billing/checkout", {}, {**H_EVIL, "Cookie": cookie_s})
    s_cus = acct(csite["cw"], "desert-supply")["stripe_customer_id"]
    s, _, _ = _req(csite["port"], "POST", "/api/billing/portal", {"customer": s_cus, "organization_id": 1},
                   {**H_EVIL, "Cookie": cookie_c})
    assert s == 409 and not any(k.get("customer") == s_cus for n, k in gw.calls if n == "create_portal_session")
