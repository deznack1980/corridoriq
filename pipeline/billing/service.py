"""Authenticated billing actions: status, Checkout, Customer Portal.

The caller's organization comes from the server-side session, never from the
request. The browser never chooses the price, amount, currency, customer,
redirect URLs or entitlement: the request body of these actions is ignored.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from pipeline.auth.rbac import AuthzError, has_permission
from pipeline.auth.seed import DEFAULT_ORG_SLUG
from pipeline.auth.service import write_audit
from pipeline.billing import store
from pipeline.billing.config import PLAN_DISPLAY_PRICE, PLAN_KEY, PLAN_NAME, BillingConfigError
from pipeline.billing.entitlements import supplier_has_paid_access
from pipeline.billing.gateway import CHECKOUT_HOSTS, PORTAL_HOSTS, BillingGatewayError, is_hosted_url
from pipeline.billing.states import LIVE_SUBSCRIPTION_STATES, BillingState

log = logging.getLogger(__name__)

PERMISSION = "billing.manage"
ACCOUNT_TYPE = "supplier"
# CorridorIQ's own organization is internal and is never billed.
NON_BILLABLE_SLUGS = frozenset({DEFAULT_ORG_SLUG})
# Reuse an open Checkout Session only if it stays valid this much longer.
REUSE_MARGIN = timedelta(minutes=10)


class BillingError(Exception):
    """Safe, user-facing billing failure with an HTTP status."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


UNAVAILABLE = "Billing is temporarily unavailable. Please try again later."


def _org(conn, user):
    row = conn.execute("SELECT id, name, slug, is_active FROM organizations WHERE id=?",
                       (user["organization_id"],)).fetchone()
    if row is None or not row["is_active"]:
        raise BillingError("Your organization is not active.", 403)
    return row


def _billable(org) -> bool:
    return org["slug"] not in NON_BILLABLE_SLUGS


def _metadata(org) -> dict:
    # Stable internal identifiers only: no names, emails or other personal data.
    return {"corridoriq_tenant_id": str(org["id"]), "corridoriq_account_type": ACCOUNT_TYPE,
            "corridoriq_plan": PLAN_KEY}


def _require_manage(user):
    if not has_permission(user, PERMISSION):
        raise AuthzError("You do not have permission to manage billing.")


def _audit(conn, user, event_type, *, success=True, ip=None, ua=None, **details):
    write_audit(conn, event_type=event_type, success=success, user_id=user["id"],
                organization_id=user["organization_id"], resource_type="billing",
                resource_id=user["organization_id"], ip_address=ip, user_agent=ua,
                details=details or None)


def _state(acct) -> BillingState:
    if acct is None:
        return BillingState.NONE
    try:
        return BillingState(acct["billing_state"])
    except ValueError:
        return BillingState.UNKNOWN


def billing_status(conn, user, config) -> dict:
    org = _org(conn, user)
    acct = store.get_account(conn, org["id"])
    state = _state(acct)
    return {
        "plan": {"key": PLAN_KEY, "name": PLAN_NAME, "display_price": PLAN_DISPLAY_PRICE},
        "mode": config.mode,
        "available": not config.problems(),
        "billable": _billable(org),
        "can_manage": has_permission(user, PERMISSION),
        "state": state.value,
        "paid_access": supplier_has_paid_access(conn, org["id"], config),
        "cancel_at_period_end": bool(acct["cancel_at_period_end"]) if acct else False,
        "current_period_end": acct["current_period_end"] if acct else None,
        "has_billing_account": bool(acct and acct["stripe_customer_id"]),
    }


def _ensure_customer(conn, org, acct, config, gateway) -> str:
    if acct["stripe_customer_id"]:
        return acct["stripe_customer_id"]
    key = f"corridoriq-{config.mode}-org-{org['id']}-customer-{acct['created_at']}"
    customer = gateway.create_customer(name=org["name"], metadata=_metadata(org), idempotency_key=key)
    cid = customer.get("id") or ""
    if not cid.startswith("cus_"):
        raise BillingGatewayError("unexpected customer response")
    if bool(customer.get("livemode")) != config.livemode:
        raise BillingGatewayError("customer created in the wrong Stripe mode")
    store.update_account(conn, org["id"], stripe_customer_id=cid)
    return cid


def start_checkout(conn, user, config, gateway, *, ip=None, ua=None) -> dict:
    """Return {"url": <Stripe-hosted Checkout URL>} for the caller's organization."""
    _require_manage(user)
    org = _org(conn, user)
    if not _billable(org):
        raise BillingError("This organization is not billed through CorridorIQ.", 403)
    try:
        config.require_ready()
    except BillingConfigError:
        log.error("billing checkout refused: configuration incomplete (%d problem(s))", len(config.problems()))
        raise BillingError(UNAVAILABLE, 503) from None

    with store.BILLING_LOCK:
        acct = store.ensure_account(conn, org["id"])
        if acct["stripe_subscription_id"] and _state(acct) in LIVE_SUBSCRIPTION_STATES:
            raise BillingError("Your organization already has a subscription. Use Manage Billing instead.", 409)
        try:
            customer_id = _ensure_customer(conn, org, acct, config, gateway)
            acct = store.get_account(conn, org["id"])

            # Duplicate-Checkout guard: resume a still-open session instead of
            # creating a second one (which could lead to two subscriptions).
            expires = acct["checkout_expires_at"]
            if acct["checkout_session_id"] and expires and \
                    datetime.fromisoformat(expires) > datetime.now(timezone.utc) + REUSE_MARGIN:
                existing = gateway.retrieve_checkout_session(acct["checkout_session_id"])
                if existing.get("status") == "open" and existing.get("customer") == customer_id \
                        and is_hosted_url(existing.get("url"), CHECKOUT_HOSTS):
                    _audit(conn, user, "billing_checkout_started", ip=ip, ua=ua, reused=True)
                    return {"url": existing["url"]}

            meta = _metadata(org)
            params = {
                "mode": "subscription",
                "customer": customer_id,
                "client_reference_id": str(org["id"]),
                "line_items": [{"price": config.price_id, "quantity": 1}],
                "success_url": config.success_url,
                "cancel_url": config.cancel_url,
                "metadata": meta,
                "subscription_data": {"metadata": meta},
            }
            session = gateway.create_checkout_session(params, idempotency_key=f"corridoriq-checkout-{uuid.uuid4()}")
        except BillingGatewayError as exc:
            log.error("billing checkout failed for organization %s: %s", org["id"], exc)
            _audit(conn, user, "billing_checkout_started", success=False, ip=ip, ua=ua, reason="stripe_error")
            raise BillingError(UNAVAILABLE, 503) from None

        url = session.get("url")
        if not is_hosted_url(url, CHECKOUT_HOSTS) or session.get("customer") != customer_id \
                or session.get("mode") != "subscription":
            log.error("billing checkout for organization %s returned an unexpected session", org["id"])
            raise BillingError(UNAVAILABLE, 503)
        fields = {"checkout_session_id": session.get("id"),
                  "checkout_expires_at": store.epoch_to_iso(session.get("expires_at"))}
        if not acct["stripe_subscription_id"] or _state(acct) not in LIVE_SUBSCRIPTION_STATES:
            fields["billing_state"] = BillingState.CHECKOUT_PENDING.value
        store.update_account(conn, org["id"], **fields)
        _audit(conn, user, "billing_checkout_started", ip=ip, ua=ua, reused=False)
        return {"url": url}


def create_portal_session(conn, user, config, gateway, *, ip=None, ua=None) -> dict:
    """Return {"url": <Stripe-hosted Customer Portal URL>} for the caller's organization."""
    _require_manage(user)
    org = _org(conn, user)
    try:
        config.require_ready()
    except BillingConfigError:
        raise BillingError(UNAVAILABLE, 503) from None
    acct = store.get_account(conn, org["id"])
    if acct is None or not acct["stripe_customer_id"]:
        raise BillingError("There is no billing account for your organization yet.", 409)
    try:
        session = gateway.create_portal_session(customer=acct["stripe_customer_id"],
                                                return_url=config.portal_return_url)
    except BillingGatewayError as exc:
        log.error("billing portal failed for organization %s: %s", org["id"], exc)
        raise BillingError(UNAVAILABLE, 503) from None
    url = session.get("url")
    if not is_hosted_url(url, PORTAL_HOSTS) or session.get("customer") != acct["stripe_customer_id"]:
        raise BillingError(UNAVAILABLE, 503)
    _audit(conn, user, "billing_portal_session_created", ip=ip, ua=ua)
    return {"url": url}
