"""Signature-verified Stripe webhook processing.

Trust model
-----------
1. The raw request body must carry a valid Stripe-Signature for
   STRIPE_WEBHOOK_SECRET, within a 5-minute replay window.
2. Each event ID is processed at most once (billing_stripe_events).
3. The organization is resolved from the Stripe Customer ID that CorridorIQ
   itself created and stored for that organization, never from metadata or
   client_reference_id. Those are only cross-checked; a mismatch is rejected.
4. Subscription state is re-fetched from the Stripe API, so the payload's
   own status fields are never trusted, and out-of-order events converge on
   Stripe's current state.

Responses: 2xx = handled (Stripe stops retrying), 400 = rejected request,
503/500 = temporary failure (Stripe retries with backoff for up to 3 days).
"""

from __future__ import annotations

import json
import logging

from pipeline.auth.service import write_audit
from pipeline.billing import store
from pipeline.billing.config import BillingConfigError
from pipeline.billing.entitlements import supplier_has_paid_access
from pipeline.billing.gateway import BillingGatewayError, WebhookSignatureError, verify_webhook_signature
from pipeline.billing.states import LIVE_SUBSCRIPTION_STATES, BillingState, map_stripe_status

log = logging.getLogger(__name__)

MAX_PAYLOAD_BYTES = 512 * 1024

SUBSCRIPTION_EVENTS = frozenset({
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "customer.subscription.paused",
    "customer.subscription.resumed",
})
INVOICE_EVENTS = frozenset({
    "invoice.paid",
    "invoice.payment_failed",
    "invoice.payment_action_required",
})
CHECKOUT_EVENTS = frozenset({"checkout.session.completed", "checkout.session.expired"})
SUPPORTED_EVENTS = SUBSCRIPTION_EVENTS | INVOICE_EVENTS | CHECKOUT_EVENTS

OK = (200, {"received": True})


def _id(value) -> str | None:
    """Stripe references may be an ID string or an expanded object."""
    if isinstance(value, dict):
        value = value.get("id")
    return value if isinstance(value, str) and value else None


def _parse_envelope(payload: bytes) -> dict | None:
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(event, dict):
        return None
    obj = (event.get("data") or {}).get("object") if isinstance(event.get("data"), dict) else None
    if not (isinstance(event.get("id"), str) and event["id"].startswith("evt_")
            and isinstance(event.get("type"), str) and isinstance(event.get("livemode"), bool)
            and isinstance(obj, dict)):
        return None
    return event


def _audit(conn, org_id, event_type, *, success=True, **details):
    write_audit(conn, event_type=event_type, success=success, organization_id=org_id,
                resource_type="billing", resource_id=org_id, action="stripe_webhook",
                details=details or None)


def _subscription_ref(event_type: str, obj: dict) -> str | None:
    if event_type in SUBSCRIPTION_EVENTS:
        return _id(obj.get("id"))
    if event_type in INVOICE_EVENTS:
        parent = obj.get("parent") or {}
        details = parent.get("subscription_details") or {} if isinstance(parent, dict) else {}
        # API 2025-03-31+ nests it under parent; older versions had invoice.subscription.
        return _id(details.get("subscription")) or _id(obj.get("subscription"))
    return _id(obj.get("subscription"))


def _period_end(sub: dict):
    if sub.get("current_period_end"):  # API versions before 2025-03-31
        return store.epoch_to_iso(sub["current_period_end"])
    ends = [i.get("current_period_end") for i in ((sub.get("items") or {}).get("data") or [])
            if isinstance(i, dict) and i.get("current_period_end")]
    return store.epoch_to_iso(max(ends)) if ends else None


def _price_ids(sub: dict) -> list[str]:
    out = []
    for item in ((sub.get("items") or {}).get("data") or []):
        pid = _id((item or {}).get("price")) if isinstance(item, dict) else None
        if pid:
            out.append(pid)
    return out


def _apply_subscription(conn, acct, subscription_id: str, customer_id: str, event_type: str,
                        config, gateway) -> str:
    org_id = acct["organization_id"]
    sub = gateway.retrieve_subscription(subscription_id)  # authoritative state
    if _id(sub.get("customer")) != customer_id:
        _audit(conn, org_id, "billing_webhook_rejected", success=False, reason="customer_mismatch", stripe_event=event_type)
        return "rejected:customer_mismatch"
    if bool(sub.get("livemode")) != config.livemode:
        _audit(conn, org_id, "billing_webhook_rejected", success=False, reason="mode_mismatch", stripe_event=event_type)
        return "rejected:mode_mismatch"

    new_state = map_stripe_status(sub.get("status"))
    stored_id = acct["stripe_subscription_id"]
    try:
        stored_state = BillingState(acct["billing_state"])
    except ValueError:
        stored_state = BillingState.UNKNOWN
    if stored_id and stored_id != sub.get("id") and stored_state in LIVE_SUBSCRIPTION_STATES:
        # A second subscription for an organization that already has a live one
        # (e.g. two Checkouts completed). Never silently overwrite: an operator
        # must review and refund/cancel one of them in Stripe.
        if new_state in LIVE_SUBSCRIPTION_STATES:
            log.error("organization %s has a second live Stripe subscription; manual review needed", org_id)
            _audit(conn, org_id, "billing_duplicate_subscription", success=False, stripe_event=event_type)
            return "ignored:duplicate_subscription"
        return "ignored:other_subscription"

    prices = _price_ids(sub)
    price_ok = config.price_id in prices
    was_paid = supplier_has_paid_access(conn, org_id, config)
    period_end = _period_end(sub)
    cancel_flag = 1 if (sub.get("cancel_at_period_end") or sub.get("cancel_at")) else 0
    changed = (acct["billing_state"] != new_state.value or acct["current_period_end"] != period_end
               or int(acct["cancel_at_period_end"] or 0) != cancel_flag or stored_id != sub.get("id"))
    store.update_account(
        conn, org_id,
        stripe_subscription_id=sub.get("id"),
        stripe_price_id=config.price_id if price_ok else (prices[0] if prices else None),
        subscription_status=str(sub.get("status") or ""),
        billing_state=new_state.value,
        livemode=1 if sub.get("livemode") else 0,
        current_period_end=period_end,
        cancel_at_period_end=cancel_flag,
        checkout_session_id=None,
        checkout_expires_at=None,
        billing_updated_at=store.now_iso(),
    )
    now_paid = supplier_has_paid_access(conn, org_id, config)

    if not price_ok:
        log.error("organization %s subscription has an unexpected price; access not granted", org_id)
        _audit(conn, org_id, "billing_price_mismatch", success=False, stripe_event=event_type, state=new_state.value)
    if now_paid and not was_paid:
        _audit(conn, org_id, "billing_subscription_activated", stripe_event=event_type, state=new_state.value)
    elif new_state is BillingState.CANCELED and stored_state is not BillingState.CANCELED:
        _audit(conn, org_id, "billing_subscription_canceled", stripe_event=event_type, state=new_state.value)
    elif event_type == "invoice.payment_failed":
        _audit(conn, org_id, "billing_payment_failed", success=False, stripe_event=event_type, state=new_state.value)
    elif changed:
        _audit(conn, org_id, "billing_subscription_updated", stripe_event=event_type, state=new_state.value,
               paid_access=now_paid)
    return "applied" if price_ok else "applied:price_mismatch"


def _handle(conn, event: dict, config, gateway) -> tuple[str, int | None]:
    event_type, obj = event["type"], event["data"]["object"]
    customer_id = _id(obj.get("customer"))
    acct = store.account_by_customer(conn, customer_id)
    if acct is None:
        # Not a customer CorridorIQ created (another integration on the same
        # Stripe account, or a deleted organization). Never guess a tenant.
        return "ignored:unknown_customer", None
    org_id = acct["organization_id"]

    if event_type in CHECKOUT_EVENTS:
        if obj.get("mode") != "subscription":
            return "ignored:not_subscription", org_id
        claimed = obj.get("client_reference_id")
        meta_claim = (obj.get("metadata") or {}).get("corridoriq_tenant_id")
        for claim in (claimed, meta_claim):
            if claim is not None and str(claim) != str(org_id):
                log.error("checkout session tenant claim does not match its customer (organization %s)", org_id)
                _audit(conn, org_id, "billing_webhook_rejected", success=False, reason="tenant_mismatch",
                       stripe_event=event_type)
                return "rejected:tenant_mismatch", org_id
        if event_type == "checkout.session.expired":
            if acct["checkout_session_id"] == _id(obj.get("id")):
                fields = {"checkout_session_id": None, "checkout_expires_at": None}
                if acct["billing_state"] == BillingState.CHECKOUT_PENDING.value:
                    fields["billing_state"] = BillingState.NONE.value
                store.update_account(conn, org_id, **fields)
            return "applied", org_id

    subscription_id = _subscription_ref(event_type, obj)
    if not subscription_id:
        return "ignored:no_subscription", org_id
    return _apply_subscription(conn, acct, subscription_id, customer_id, event_type, config, gateway), org_id


def handle_webhook(conn, payload: bytes, sig_header: str | None, config, gateway) -> tuple[int, dict]:
    try:
        config.require_webhook_ready()
    except BillingConfigError:
        log.error("stripe webhook refused: billing webhook configuration incomplete")
        return 503, {"error": "billing unavailable"}
    if not payload or len(payload) > MAX_PAYLOAD_BYTES:
        return 400, {"error": "invalid payload"}
    try:
        verify_webhook_signature(payload, sig_header, config.webhook_secret)
    except WebhookSignatureError:
        log.warning("stripe webhook rejected: invalid signature")
        return 400, {"error": "invalid signature"}
    event = _parse_envelope(payload)
    if event is None:
        return 400, {"error": "invalid payload"}
    if event["livemode"] != config.livemode:
        log.error("stripe webhook rejected: event mode does not match configured keys")
        return 400, {"error": "mode mismatch"}

    with store.BILLING_LOCK:
        claim = store.begin_event(conn, event["id"], event["type"], event["livemode"])
        if claim == "duplicate":
            return 200, {"received": True, "duplicate": True}
        if event["type"] not in SUPPORTED_EVENTS:
            store.finish_event(conn, event["id"], "ignored:unsupported_type")
            return OK
        try:
            outcome, org_id = _handle(conn, event, config, gateway)
        except BillingGatewayError as exc:
            store.finish_event(conn, event["id"], "error")
            log.error("stripe webhook %s could not be applied (will be retried): %s", event["type"], exc)
            return 503, {"error": "temporarily unavailable"}
        except Exception:
            store.finish_event(conn, event["id"], "error")
            log.exception("stripe webhook %s failed (will be retried)", event["type"])
            return 500, {"error": "internal error"}
        store.finish_event(conn, event["id"], outcome, org_id)
        return OK
