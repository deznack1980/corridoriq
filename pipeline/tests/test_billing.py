"""Supplier billing (Stripe): config, Checkout, Portal, webhooks, entitlements.

No test reaches Stripe. A fake gateway stands in for the SDK adapter, an
autouse guard makes constructing the real client fail, and webhook signatures
are produced locally with the same HMAC scheme Stripe uses.
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer

import pytest

from pipeline.auth import service as auth
from pipeline.auth.rbac import AuthzError, load_user_permissions
from pipeline.auth.seed import seed_auth
from pipeline.billing import entitlements, service, store
from pipeline.billing import webhook as wh
from pipeline.billing.__main__ import check_config, verify_price
from pipeline.billing.config import load_config
from pipeline.billing.gateway import BillingGatewayError
from pipeline.billing.states import BillingState, map_stripe_status
from pipeline.config.settings import SCHEMA_PATH

PRICE = "price_TESTfoundingsupply"
WHSEC = "whsec_test_corridoriq_unit"
SK = "sk_test_unit_not_a_real_key"
BASE = "http://127.0.0.1:8780"
ENV = {
    "CORRIDORIQ_BILLING_ENABLED": "1",
    "STRIPE_SECRET_KEY": SK,
    "STRIPE_PUBLISHABLE_KEY": "pk_test_unit",
    "STRIPE_WEBHOOK_SECRET": WHSEC,
    "STRIPE_FOUNDING_SUPPLY_PRICE_ID": PRICE,
    "STRIPE_FOUNDING_SUPPLY_PRICE_LOOKUP_KEY": "corridoriq_founding_supply_partner_monthly",
    "CORRIDORIQ_PUBLIC_BASE_URL": BASE,
}


# ---------------------------------------------------------------- fixtures --
@pytest.fixture(autouse=True)
def _no_real_stripe(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("tests must never construct the real Stripe client")
    monkeypatch.setattr("pipeline.billing.gateway.StripeGateway.__init__", boom)


class FakeGateway:
    def __init__(self, livemode=False):
        self.livemode = livemode
        self.calls = []
        self.subscriptions = {}
        self.sessions = {}
        self.fail = False
        self.portal_url = "https://billing.stripe.com/p/session/test_123"
        self._n = 0

    def _check(self, _op, **kw):
        self.calls.append((_op, kw))
        if self.fail:
            raise BillingGatewayError("stripe APIConnectionError request_id=None")

    def create_customer(self, *, name, metadata, idempotency_key):
        self._check("create_customer", name=name, metadata=metadata, idempotency_key=idempotency_key)
        self._n += 1
        return {"id": f"cus_test{self._n}", "livemode": self.livemode, "metadata": metadata}

    def create_checkout_session(self, params, *, idempotency_key):
        self._check("create_checkout_session", params=params, idempotency_key=idempotency_key)
        self._n += 1
        sid = f"cs_test_{self._n}"
        s = {"id": sid, "url": f"https://checkout.stripe.com/c/pay/{sid}", "customer": params["customer"],
             "mode": "subscription", "status": "open",
             "expires_at": int(time.time()) + 24 * 3600}
        self.sessions[sid] = s
        return s

    def retrieve_checkout_session(self, session_id):
        self._check("retrieve_checkout_session", session_id=session_id)
        return self.sessions[session_id]

    def create_portal_session(self, *, customer, return_url):
        self._check("create_portal_session", customer=customer, return_url=return_url)
        return {"url": self.portal_url, "customer": customer}

    def retrieve_subscription(self, subscription_id):
        self._check("retrieve_subscription", subscription_id=subscription_id)
        return self.subscriptions[subscription_id]

    def retrieve_price(self, price_id):
        self._check("retrieve_price", price_id=price_id)
        return {"id": price_id, "livemode": self.livemode, "active": True, "type": "recurring",
                "recurring": {"interval": "month", "interval_count": 1}, "unit_amount": 75000,
                "currency": "usd", "lookup_key": "corridoriq_founding_supply_partner_monthly",
                "product": "prod_test"}


def _factory(path):
    def f():
        c = sqlite3.connect(path, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c
    return f


@pytest.fixture()
def world(tmp_path):
    factory = _factory(tmp_path / "billing.db")
    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(c)
    now = store.now_iso()
    house = c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    orgs = {"house": house}
    for slug, name in (("desert-supply", "Desert Supply Co"), ("mesa-pipe", "Mesa Pipe & Valve")):
        orgs[slug] = c.execute("INSERT INTO organizations (name, slug, is_active, created_at, updated_at) "
                               "VALUES (?, ?, 1, ?, ?)", (name, slug, now, now)).lastrowid
    c.commit()
    users = {}
    for key, org, role in (("admin", house, "admin"), ("mgr_a", orgs["desert-supply"], "sales_manager"),
                           ("rep_a", orgs["desert-supply"], "sales_representative"),
                           ("mgr_b", orgs["mesa-pipe"], "sales_manager")):
        uid = auth.create_user(c, organization_id=org, email=f"{key}@example.com", password="Passw0rd!x",
                               role_names=[role], must_change_password=False)
        users[key] = uid
    c.close()
    return {"factory": factory, "orgs": orgs, "users": users, "db": tmp_path / "billing.db"}


def _ctx(world, key):
    c = world["factory"]()
    try:
        return auth.build_user_context(c, c.execute("SELECT * FROM users WHERE id=?",
                                                    (world["users"][key],)).fetchone())
    finally:
        c.close()


def cfg(**over):
    env = dict(ENV)
    env.update(over)
    return load_config({k: v for k, v in env.items() if v is not None})


def _period(days=30):
    return int((datetime.now(timezone.utc) + timedelta(days=days)).timestamp())


def sub(sid="sub_1", customer="cus_test1", status="active", price=PRICE, livemode=False, cancel=False, days=30):
    # API 2026-06-24.dahlia shape: current_period_end lives on the items.
    return {"id": sid, "object": "subscription", "customer": customer, "status": status,
            "livemode": livemode, "cancel_at_period_end": cancel, "cancel_at": None,
            "items": {"data": [{"price": {"id": price}, "current_period_end": _period(days)}]}}


def sign(payload: bytes, secret=WHSEC, ts=None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


_EVT = [0]


def event(etype, obj, *, eid=None, livemode=False) -> bytes:
    _EVT[0] += 1
    return json.dumps({"id": eid or f"evt_test_{_EVT[0]}", "object": "event", "type": etype,
                       "livemode": livemode, "api_version": "2026-06-24.dahlia",
                       "data": {"object": obj}}).encode()


def deliver(world, payload, gateway, config=None, header=None):
    c = world["factory"]()
    try:
        return wh.handle_webhook(c, payload, sign(payload) if header is None else header,
                                 config or cfg(), gateway)
    finally:
        c.close()


def account(world, org_key):
    c = world["factory"]()
    try:
        return store.get_account(c, world["orgs"][org_key])
    finally:
        c.close()


def audit_events(world, org_key):
    c = world["factory"]()
    try:
        return [r["event_type"] for r in c.execute(
            "SELECT event_type FROM security_audit_log WHERE organization_id=? AND event_type LIKE 'billing_%' "
            "ORDER BY id", (world["orgs"][org_key],))]
    finally:
        c.close()


def checkout(world, key="mgr_a", gateway=None, config=None):
    gw = gateway or FakeGateway()
    c = world["factory"]()
    try:
        return service.start_checkout(c, _ctx(world, key), config or cfg(), gw), gw
    finally:
        c.close()


def paid(world, org_key, config=None):
    c = world["factory"]()
    try:
        return entitlements.supplier_has_paid_access(c, world["orgs"][org_key], config or cfg())
    finally:
        c.close()


def subscribe(world, gw, org_key="desert-supply", status="active", **kw):
    """Checkout + checkout.session.completed for org; returns the subscription id."""
    checkout(world, gateway=gw) if org_key == "desert-supply" else checkout(world, "mgr_b", gateway=gw)
    cus = account(world, org_key)["stripe_customer_id"]
    sid = f"sub_{org_key}"
    gw.subscriptions[sid] = sub(sid, cus, status=status, **kw)
    payload = event("checkout.session.completed", {
        "id": "cs_x", "object": "checkout.session", "mode": "subscription", "customer": cus,
        "subscription": sid, "client_reference_id": str(world["orgs"][org_key]),
        "metadata": {"corridoriq_tenant_id": str(world["orgs"][org_key])}})
    assert deliver(world, payload, gw)[0] == 200
    return sid


# ------------------------------------------------------------------ config --
def test_config_valid_test_mode_and_secrets_never_in_repr():
    c = cfg()
    assert c.problems() == [] and c.mode == "test" and not c.livemode
    assert SK not in repr(c) and WHSEC not in repr(c)


@pytest.mark.parametrize("over,needle", [
    ({"STRIPE_SECRET_KEY": "sk_live_x"}, "only accepted with CORRIDORIQ_ENV=production"),
    ({"STRIPE_PUBLISHABLE_KEY": "pk_live_x"}, "different modes"),
    ({"STRIPE_SECRET_KEY": "not_a_key"}, "not a Stripe secret"),
    ({"STRIPE_FOUNDING_SUPPLY_PRICE_ID": "prod_VNrzzzFA4mhRce"}, "not a Stripe price ID"),
    ({"CORRIDORIQ_PUBLIC_BASE_URL": "http://corridoriq.pro"}, "must use https"),
    ({"CORRIDORIQ_PUBLIC_BASE_URL": "https://corridoriq.pro/evil?next=x"}, "origin only"),
    ({"CORRIDORIQ_BILLING_ENABLED": "0"}, "CORRIDORIQ_BILLING_ENABLED"),
])
def test_config_problems_fail_closed(over, needle):
    problems = cfg(**over).problems()
    assert any(needle in p for p in problems), problems


def test_live_keys_accepted_only_in_production_with_https():
    live = dict(STRIPE_SECRET_KEY="sk_live_x", STRIPE_PUBLISHABLE_KEY="pk_live_x", CORRIDORIQ_ENV="production")
    assert cfg(**live, CORRIDORIQ_PUBLIC_BASE_URL="https://corridoriq.pro").problems() == []
    assert cfg(**live, CORRIDORIQ_PUBLIC_BASE_URL="https://corridoriq.pro").livemode
    assert cfg(**live).problems()  # http://127.0.0.1 is not acceptable with live keys


def test_check_config_output_never_prints_secrets(capsys):
    assert check_config(cfg()) == 0
    out = capsys.readouterr().out
    assert SK not in out and WHSEC not in out and "pk_test_unit" not in out


def test_verify_price_refuses_live_without_flag_and_checks_lookup_key(capsys):
    live = cfg(STRIPE_SECRET_KEY="sk_live_x", STRIPE_PUBLISHABLE_KEY="pk_live_x", CORRIDORIQ_ENV="production",
               CORRIDORIQ_PUBLIC_BASE_URL="https://corridoriq.pro")
    gw = FakeGateway(livemode=True)
    assert verify_price(live, allow_live=False, gateway=gw) == 1 and gw.calls == []
    assert verify_price(cfg(), allow_live=False, gateway=FakeGateway()) == 0


def test_status_mapping_is_explicit_and_unknown_fails_closed():
    assert map_stripe_status("active") is BillingState.ACTIVE
    assert map_stripe_status("past_due") is BillingState.PAST_DUE
    assert map_stripe_status("canceled") is BillingState.CANCELED
    assert map_stripe_status("some_future_status") is BillingState.UNKNOWN
    assert map_stripe_status(None) is BillingState.UNKNOWN


# ---------------------------------------------------------------- checkout --
def test_unauthorized_supplier_user_cannot_start_checkout(world):
    with pytest.raises(AuthzError):
        checkout(world, "rep_a")


def test_corridoriq_house_org_is_never_billed(world):
    with pytest.raises(service.BillingError) as e:
        checkout(world, "admin")
    assert e.value.status == 403


def test_checkout_uses_server_price_metadata_and_fixed_urls(world):
    result, gw = checkout(world)
    assert result["url"].startswith("https://checkout.stripe.com/")
    (_, cust), (_, sess) = [c for c in gw.calls if c[0] in ("create_customer", "create_checkout_session")]
    org = str(world["orgs"]["desert-supply"])
    assert cust["metadata"] == {"corridoriq_tenant_id": org, "corridoriq_account_type": "supplier",
                                "corridoriq_plan": "founding_supply_partner"}
    p = sess["params"]
    assert p["mode"] == "subscription" and p["line_items"] == [{"price": PRICE, "quantity": 1}]
    assert p["client_reference_id"] == org and p["metadata"]["corridoriq_tenant_id"] == org
    assert p["subscription_data"]["metadata"]["corridoriq_tenant_id"] == org
    assert p["success_url"] == f"{BASE}/billing.html?checkout=success&session_id={{CHECKOUT_SESSION_ID}}"
    assert p["cancel_url"] == f"{BASE}/billing.html?checkout=canceled"
    assert "amount" not in json.dumps(p) and "price_data" not in p
    acct = account(world, "desert-supply")
    assert acct["billing_state"] == "checkout_pending" and acct["stripe_customer_id"] == "cus_test1"
    assert "billing_checkout_started" in audit_events(world, "desert-supply")


def test_customer_is_reused_and_open_session_is_resumed(world):
    first, gw = checkout(world)
    second, _ = checkout(world, gateway=gw)
    assert [n for n, _ in gw.calls].count("create_customer") == 1
    assert [n for n, _ in gw.calls].count("create_checkout_session") == 1
    assert second["url"] == first["url"]


def test_expired_session_is_replaced_but_customer_kept(world):
    _, gw = checkout(world)
    c = world["factory"]()
    store.update_account(c, world["orgs"]["desert-supply"], checkout_expires_at=store.now_iso())
    c.close()
    checkout(world, gateway=gw)
    names = [n for n, _ in gw.calls]
    assert names.count("create_customer") == 1 and names.count("create_checkout_session") == 2


def test_checkout_refused_when_subscription_already_live(world):
    gw = FakeGateway()
    subscribe(world, gw)
    with pytest.raises(service.BillingError) as e:
        checkout(world, gateway=gw)
    assert e.value.status == 409


def test_checkout_fails_closed_on_missing_config_and_stripe_outage(world):
    with pytest.raises(service.BillingError) as e:
        checkout(world, config=cfg(STRIPE_SECRET_KEY=None))
    assert e.value.status == 503
    gw = FakeGateway()
    gw.fail = True
    with pytest.raises(service.BillingError) as e:
        checkout(world, gateway=gw)
    assert e.value.status == 503 and SK not in str(e.value)
    assert account(world, "desert-supply")["billing_state"] == "none"


def test_checkout_rejects_customer_from_wrong_mode(world):
    with pytest.raises(service.BillingError):
        checkout(world, gateway=FakeGateway(livemode=True))
    assert account(world, "desert-supply")["stripe_customer_id"] is None


def test_checkout_rejects_non_stripe_redirect_from_gateway(world):
    gw = FakeGateway()
    orig = gw.create_checkout_session
    gw.create_checkout_session = lambda params, **k: {**orig(params, **k), "url": "https://evil.example/pay"}
    with pytest.raises(service.BillingError):
        checkout(world, gateway=gw)


# ------------------------------------------------------------------ portal --
def _portal(world, key, gw):
    c = world["factory"]()
    try:
        return service.create_portal_session(c, _ctx(world, key), cfg(), gw)
    finally:
        c.close()


def test_portal_requires_billing_customer(world):
    with pytest.raises(service.BillingError) as e:
        _portal(world, "mgr_a", FakeGateway())
    assert e.value.status == 409


def test_portal_uses_own_customer_and_fixed_return_url(world):
    gw = FakeGateway()
    checkout(world, gateway=gw)
    result = _portal(world, "mgr_a", gw)
    assert result["url"].startswith("https://billing.stripe.com/")
    name, kw = gw.calls[-1]
    assert name == "create_portal_session" and kw == {"customer": "cus_test1", "return_url": f"{BASE}/billing.html"}
    assert "billing_portal_session_created" in audit_events(world, "desert-supply")


def test_portal_cross_tenant_is_impossible(world):
    gw = FakeGateway()
    checkout(world, gateway=gw)  # org A now has cus_test1
    with pytest.raises(service.BillingError) as e:
        _portal(world, "mgr_b", gw)  # org B has no customer of its own
    assert e.value.status == 409
    assert not any(k.get("customer") == "cus_test1" for n, k in gw.calls if n == "create_portal_session")


def test_portal_rejects_non_stripe_url_and_requires_permission(world):
    gw = FakeGateway()
    checkout(world, gateway=gw)
    gw.portal_url = "https://evil.example/portal"
    with pytest.raises(service.BillingError):
        _portal(world, "mgr_a", gw)
    with pytest.raises(AuthzError):
        _portal(world, "rep_a", gw)


# ----------------------------------------------------------------- webhook --
def test_invalid_signature_rejected_without_side_effects(world):
    gw = FakeGateway()
    payload = event("customer.subscription.updated", sub())
    assert deliver(world, payload, gw, header=sign(payload, secret="whsec_wrong"))[0] == 400
    assert deliver(world, payload, gw, header="")[0] == 400
    assert deliver(world, payload, gw, header="t=1,v1=deadbeef")[0] == 400
    assert gw.calls == []


def test_replayed_old_signature_rejected(world):
    payload = event("customer.subscription.updated", sub())
    old = sign(payload, ts=int(time.time()) - 3600)
    assert deliver(world, payload, FakeGateway(), header=old)[0] == 400


def test_tampered_payload_rejected(world):
    payload = event("customer.subscription.updated", sub())
    header = sign(payload)
    assert deliver(world, payload.replace(b"active", b"ACTIVE"), FakeGateway(), header=header)[0] == 400


@pytest.mark.parametrize("payload", [b"not json", b"[]", b'{"id":"evt_1"}',
                                     b'{"id":"x","type":"a","livemode":false,"data":{"object":{}}}', b""])
def test_malformed_payload_rejected(world, payload):
    assert deliver(world, payload, FakeGateway())[0] == 400


def test_webhook_fails_closed_without_secret(world):
    payload = event("invoice.paid", {})
    assert deliver(world, payload, FakeGateway(), config=cfg(STRIPE_WEBHOOK_SECRET=None))[0] == 503


def test_livemode_mismatch_rejected(world):
    payload = event("customer.subscription.updated", sub(), livemode=True)
    assert deliver(world, payload, FakeGateway())[0] == 400


def test_checkout_completion_activates_from_fetched_subscription(world):
    gw = FakeGateway()
    assert not paid(world, "desert-supply")
    subscribe(world, gw)
    acct = account(world, "desert-supply")
    assert acct["billing_state"] == "active" and acct["stripe_subscription_id"] == "sub_desert-supply"
    assert acct["stripe_price_id"] == PRICE and acct["checkout_session_id"] is None
    assert paid(world, "desert-supply") and not paid(world, "mesa-pipe")
    assert ("retrieve_subscription", {"subscription_id": "sub_desert-supply"}) in gw.calls
    assert "billing_subscription_activated" in audit_events(world, "desert-supply")


def test_payload_status_is_not_trusted(world):
    """A payload claiming 'active' does not grant access when Stripe says otherwise."""
    gw = FakeGateway()
    checkout(world, gateway=gw)
    gw.subscriptions["sub_1"] = sub("sub_1", "cus_test1", status="incomplete")
    deliver(world, event("customer.subscription.updated", sub("sub_1", "cus_test1", status="active")), gw)
    assert account(world, "desert-supply")["billing_state"] == "incomplete"
    assert not paid(world, "desert-supply")


def test_duplicate_event_is_processed_once(world):
    gw = FakeGateway()
    sid = subscribe(world, gw)
    gw.subscriptions[sid]["status"] = "past_due"
    payload = event("invoice.payment_failed", {"id": "in_1", "customer": "cus_test1",
                                               "parent": {"subscription_details": {"subscription": sid}}},
                    eid="evt_dup_1")
    assert deliver(world, payload, gw) == (200, {"received": True})
    calls = len(gw.calls)
    assert deliver(world, payload, gw) == (200, {"received": True, "duplicate": True})
    assert len(gw.calls) == calls
    assert audit_events(world, "desert-supply").count("billing_payment_failed") == 1


def test_payment_failure_removes_paid_access(world):
    gw = FakeGateway()
    sid = subscribe(world, gw)
    gw.subscriptions[sid]["status"] = "past_due"
    deliver(world, event("invoice.payment_failed", {"customer": "cus_test1",
                                                    "parent": {"subscription_details": {"subscription": sid}}}), gw)
    assert account(world, "desert-supply")["billing_state"] == "past_due"
    assert not paid(world, "desert-supply")
    assert "billing_payment_failed" in audit_events(world, "desert-supply")


def test_invoice_paid_restores_access_legacy_invoice_shape(world):
    gw = FakeGateway()
    sid = subscribe(world, gw, status="past_due")
    gw.subscriptions[sid]["status"] = "active"
    deliver(world, event("invoice.paid", {"customer": "cus_test1", "subscription": sid}), gw)
    assert paid(world, "desert-supply")


def test_subscription_update_records_cancel_at_period_end(world):
    gw = FakeGateway()
    sid = subscribe(world, gw)
    gw.subscriptions[sid]["cancel_at_period_end"] = True
    deliver(world, event("customer.subscription.updated", gw.subscriptions[sid]), gw)
    acct = account(world, "desert-supply")
    assert acct["cancel_at_period_end"] == 1 and paid(world, "desert-supply")  # paid until period end
    assert "billing_subscription_updated" in audit_events(world, "desert-supply")


def test_cancellation_ends_access(world):
    gw = FakeGateway()
    sid = subscribe(world, gw)
    gw.subscriptions[sid]["status"] = "canceled"
    deliver(world, event("customer.subscription.deleted", gw.subscriptions[sid]), gw)
    assert account(world, "desert-supply")["billing_state"] == "canceled"
    assert not paid(world, "desert-supply")
    assert "billing_subscription_canceled" in audit_events(world, "desert-supply")


def test_out_of_order_events_converge_on_stripe_state(world):
    gw = FakeGateway()
    sid = subscribe(world, gw)
    gw.subscriptions[sid]["status"] = "canceled"
    # A stale "created" event arrives after cancellation: Stripe's current state wins.
    deliver(world, event("customer.subscription.created", sub(sid, "cus_test1", status="active")), gw)
    assert account(world, "desert-supply")["billing_state"] == "canceled"


def test_unknown_customer_is_ignored(world):
    gw = FakeGateway()
    gw.subscriptions["sub_other"] = sub("sub_other", "cus_someone_else")
    status, _ = deliver(world, event("customer.subscription.created", gw.subscriptions["sub_other"]), gw)
    assert status == 200 and gw.calls == []
    c = world["factory"]()
    row = c.execute("SELECT outcome, organization_id FROM billing_stripe_events ORDER BY rowid DESC").fetchone()
    c.close()
    assert row["outcome"] == "ignored:unknown_customer" and row["organization_id"] is None


def test_wrong_price_never_grants_access(world):
    gw = FakeGateway()
    subscribe(world, gw, price="price_SOMETHING_ELSE")
    acct = account(world, "desert-supply")
    assert acct["billing_state"] == "active" and acct["stripe_price_id"] == "price_SOMETHING_ELSE"
    assert not paid(world, "desert-supply")
    assert "billing_price_mismatch" in audit_events(world, "desert-supply")


def test_cross_tenant_checkout_claim_rejected(world):
    gw = FakeGateway()
    checkout(world, gateway=gw)  # org A -> cus_test1
    gw.subscriptions["sub_x"] = sub("sub_x", "cus_test1")
    payload = event("checkout.session.completed", {
        "mode": "subscription", "customer": "cus_test1", "subscription": "sub_x",
        "client_reference_id": str(world["orgs"]["mesa-pipe"])})  # claims org B
    assert deliver(world, payload, gw)[0] == 200
    assert account(world, "desert-supply")["stripe_subscription_id"] is None
    assert account(world, "mesa-pipe") is None or account(world, "mesa-pipe")["stripe_subscription_id"] is None
    assert not paid(world, "desert-supply") and not paid(world, "mesa-pipe")
    assert "billing_webhook_rejected" in audit_events(world, "desert-supply")


def test_subscription_for_other_customer_rejected(world):
    gw = FakeGateway()
    checkout(world, gateway=gw)
    gw.subscriptions["sub_y"] = sub("sub_y", "cus_attacker")  # Stripe says another customer owns it
    deliver(world, event("customer.subscription.updated", {"id": "sub_y", "customer": "cus_test1"}), gw)
    assert account(world, "desert-supply")["stripe_subscription_id"] is None


def test_second_live_subscription_does_not_overwrite_first(world):
    gw = FakeGateway()
    sid = subscribe(world, gw)
    gw.subscriptions["sub_second"] = sub("sub_second", "cus_test1")
    deliver(world, event("customer.subscription.created", gw.subscriptions["sub_second"]), gw)
    assert account(world, "desert-supply")["stripe_subscription_id"] == sid
    assert "billing_duplicate_subscription" in audit_events(world, "desert-supply")


def test_stripe_outage_returns_retryable_error_then_succeeds(world):
    gw = FakeGateway()
    checkout(world, gateway=gw)
    gw.subscriptions["sub_1"] = sub("sub_1", "cus_test1")
    payload = event("customer.subscription.created", gw.subscriptions["sub_1"], eid="evt_retry")
    gw.fail = True
    assert deliver(world, payload, gw)[0] == 503
    assert not paid(world, "desert-supply")
    gw.fail = False
    assert deliver(world, payload, gw)[0] == 200  # Stripe's retry is processed, not treated as a duplicate
    assert paid(world, "desert-supply")


def test_unsupported_event_type_acknowledged_and_ignored(world):
    gw = FakeGateway()
    assert deliver(world, event("charge.refunded", {"customer": "cus_test1"}), gw) == (200, {"received": True})
    assert gw.calls == []


def test_checkout_expired_clears_pending_state(world):
    gw = FakeGateway()
    checkout(world, gateway=gw)
    sid = account(world, "desert-supply")["checkout_session_id"]
    deliver(world, event("checkout.session.expired", {"id": sid, "mode": "subscription", "customer": "cus_test1",
                                                      "client_reference_id": str(world["orgs"]["desert-supply"])}), gw)
    acct = account(world, "desert-supply")
    assert acct["billing_state"] == "none" and acct["checkout_session_id"] is None


def test_event_log_stores_no_payload(world):
    gw = FakeGateway()
    subscribe(world, gw)
    c = world["factory"]()
    cols = {r[1] for r in c.execute("PRAGMA table_info(billing_stripe_events)")}
    details = " ".join(r[0] or "" for r in c.execute(
        "SELECT details_json FROM security_audit_log WHERE event_type LIKE 'billing_%'"))
    c.close()
    assert cols == {"event_id", "event_type", "livemode", "organization_id", "outcome", "received_at", "processed_at"}
    assert "cus_test1" not in details and WHSEC not in details and SK not in details


# ------------------------------------------------------------ entitlements --
def test_entitlements_fail_closed(world):
    gw = FakeGateway()
    subscribe(world, gw)
    assert entitlements.entitlements_for(world["factory"](), world["orgs"]["desert-supply"], cfg()) == \
        frozenset({"supplier_intelligence"})
    assert not paid(world, "desert-supply", cfg(STRIPE_FOUNDING_SUPPLY_PRICE_ID=None))   # config missing
    assert not paid(world, "desert-supply", cfg(STRIPE_SECRET_KEY=None))                # mode unknown
    live = cfg(STRIPE_SECRET_KEY="sk_live_x", CORRIDORIQ_ENV="production",
               CORRIDORIQ_PUBLIC_BASE_URL="https://corridoriq.pro", STRIPE_PUBLISHABLE_KEY=None)
    assert not paid(world, "desert-supply", live)                                        # test state, live config
    c = world["factory"]()
    later = datetime.now(timezone.utc) + timedelta(days=40)
    assert not entitlements.supplier_has_paid_access(c, world["orgs"]["desert-supply"], cfg(), now=later)  # stale
    c.execute("UPDATE organizations SET is_active=0 WHERE id=?", (world["orgs"]["desert-supply"],))
    c.commit()
    assert not entitlements.supplier_has_paid_access(c, world["orgs"]["desert-supply"], cfg())  # inactive org
    c.close()
    broken = sqlite3.connect(":memory:")  # no tables: database unavailable
    assert entitlements.supplier_has_paid_access(broken, 1, cfg()) is False


def test_billing_permission_granted_only_to_admin_and_sales_manager(world):
    c = world["factory"]()
    try:
        assert "billing.manage" in load_user_permissions(c, world["users"]["mgr_a"])
        assert "billing.manage" not in load_user_permissions(c, world["users"]["rep_a"])
    finally:
        c.close()


# --------------------------------------------------------- HTTP / routing --
@pytest.fixture()
def site(world, monkeypatch):
    from pipeline.api import server as server_mod

    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    gw = FakeGateway()
    monkeypatch.setattr(server_mod, "_factory", world["factory"])
    monkeypatch.setattr(server_mod, "_billing_gateway", lambda config: gw)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"port": srv.server_address[1], "gw": gw, "world": world}
    srv.shutdown()


def _req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    data = body if isinstance(body, bytes) or body is None else json.dumps(body).encode()
    c.request(method, path, body=data, headers=headers or {})
    r = c.getresponse()
    raw = r.read()
    out = (r.status, {k.lower(): v for k, v in r.getheaders()}, raw)
    c.close()
    return out


def _login(port, email):
    status, headers, _ = _req(port, "POST", "/api/auth/login",
                              {"email": email, "password": "Passw0rd!x"}, {"Content-Type": "application/json"})
    assert status == 200
    return headers["set-cookie"].split(";")[0]


def test_http_billing_disabled_by_default(world, monkeypatch, site):
    monkeypatch.setenv("CORRIDORIQ_BILLING_ENABLED", "0")
    for method, path in (("GET", "/api/billing/status"), ("POST", "/api/billing/checkout"),
                         ("POST", "/api/billing/portal"), ("POST", "/api/billing/stripe/webhook"),
                         ("GET", "/billing.html"), ("GET", "/billing.js")):
        assert _req(site["port"], method, path, b"{}", {"Content-Type": "application/json"})[0] == 404, path


def test_http_unauthenticated_checkout_rejected(site):
    status, _, _ = _req(site["port"], "POST", "/api/billing/checkout", {}, {"Content-Type": "application/json"})
    assert status == 401 and site["gw"].calls == []


def test_http_checkout_ignores_client_price_and_host(site):
    cookie = _login(site["port"], "mgr_a@example.com")
    status, _, body = _req(site["port"], "POST", "/api/billing/checkout",
                           {"price": "price_attacker", "price_id": "price_attacker", "amount": 1, "quantity": 99,
                            "success_url": "https://evil.example/", "customer": "cus_victim"},
                           {"Content-Type": "application/json", "Cookie": cookie, "Host": "evil.example"})
    assert status == 200 and json.loads(body)["url"].startswith("https://checkout.stripe.com/")
    params = [k for n, k in site["gw"].calls if n == "create_checkout_session"][0]["params"]
    assert params["line_items"] == [{"price": PRICE, "quantity": 1}] and params["customer"] == "cus_test1"
    assert params["success_url"].startswith(BASE + "/") and "evil" not in json.dumps(params)


def test_http_checkout_requires_json_content_type(site):
    cookie = _login(site["port"], "mgr_a@example.com")
    status, _, _ = _req(site["port"], "POST", "/api/billing/checkout", b"x=1",
                        {"Content-Type": "application/x-www-form-urlencoded", "Cookie": cookie})
    assert status == 415 and site["gw"].calls == []


def test_http_rep_forbidden_and_status_visible(site):
    cookie = _login(site["port"], "rep_a@example.com")
    assert _req(site["port"], "POST", "/api/billing/portal", {}, {"Content-Type": "application/json",
                                                                  "Cookie": cookie})[0] == 403
    status, _, body = _req(site["port"], "GET", "/api/billing/status", None, {"Cookie": cookie})
    data = json.loads(body)
    assert status == 200 and data["can_manage"] is False and data["paid_access"] is False
    assert data["plan"]["display_price"] == "$750/month" and SK not in body.decode()


def test_http_webhook_end_to_end_and_no_cookie(site):
    cookie = _login(site["port"], "mgr_a@example.com")
    _req(site["port"], "POST", "/api/billing/checkout", {}, {"Content-Type": "application/json", "Cookie": cookie})
    site["gw"].subscriptions["sub_h"] = sub("sub_h", "cus_test1")
    payload = event("customer.subscription.created", site["gw"].subscriptions["sub_h"])
    status, headers, _ = _req(site["port"], "POST", "/api/billing/stripe/webhook", payload,
                              {"Content-Type": "application/json", "Stripe-Signature": sign(payload)})
    assert status == 200 and "set-cookie" not in headers
    assert _req(site["port"], "POST", "/api/billing/stripe/webhook", payload,
                {"Stripe-Signature": sign(payload, secret="whsec_wrong")})[0] == 400
    assert _req(site["port"], "GET", "/api/billing/stripe/webhook")[0] == 405
    data = json.loads(_req(site["port"], "GET", "/api/billing/status", None, {"Cookie": cookie})[2])
    assert data["state"] == "active" and data["paid_access"] is True


def test_http_billing_page_served_only_when_enabled_and_env_example_never(site):
    assert _req(site["port"], "GET", "/billing.html")[0] == 200
    for path in ("/.env.example", "/.env", "/pipeline/billing/config.py"):
        assert _req(site["port"], "GET", path)[0] == 404, path
