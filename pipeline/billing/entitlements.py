"""Application entitlements derived from billing state.

    BILLING STATE (what Stripe says)  !=  ENTITLEMENT (what CorridorIQ grants)

This module is the only place that turns billing state into access. Callers
ask `supplier_has_paid_access()`, `contractor_has_pro()` or
`entitlements_for()`; nothing else in CorridorIQ should compare Stripe
statuses. Every doubt resolves to "no paid access".

A plan's entitlement is granted only to an organization whose account type is
that plan's audience: a contractor subscription can never yield
supplier_intelligence, and a supplier subscription can never yield
contractor_pro.

Not yet wired into any existing access check: current production access is
unchanged until that is separately approved.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pipeline.billing import store
from pipeline.billing.config import load_config
from pipeline.billing.plans import (CONTRACTOR_PRO, CONTRACTOR_PRO_ENTITLEMENT, FOUNDING_SUPPLY_PARTNER, PLANS,
                                    SUPPLIER_INTELLIGENCE, Plan)
from pipeline.billing.states import PAID_ACCESS_STATES, BillingState

log = logging.getLogger(__name__)

__all__ = ["SUPPLIER_INTELLIGENCE", "CONTRACTOR_PRO_ENTITLEMENT", "PLAN_ENTITLEMENTS", "PRIORITY", "STANDARD",
           "has_paid_access", "supplier_has_paid_access", "contractor_has_pro", "entitlements_for",
           "request_priority"]

# Material-request priority classes (the request_priority() contract).
PRIORITY = "priority"
STANDARD = "standard"

# Every entitlement any plan can grant.
PLAN_ENTITLEMENTS = frozenset(p.entitlement for p in PLANS.values())

# A renewal webhook is normally received within minutes of the period end.
# If none has arrived this long after the recorded period end, stop granting
# access rather than trust state that may be stale.
STALE_PERIOD_GRACE = timedelta(days=3)


def _parse(ts):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def has_paid_access(conn, organization_id, plan: Plan, config=None, *, now=None) -> bool:
    """True only if `plan` is paid for, by an organization eligible for it."""
    try:
        cfg = config or load_config()
        price = cfg.price_id_for(plan)
        if not price.startswith("price_") or cfg.mode is None:
            return False
        org = conn.execute("SELECT is_active, account_type FROM organizations WHERE id=?",
                           (organization_id,)).fetchone()
        if org is None or not org["is_active"] or org["account_type"] != plan.audience:
            return False
        acct = store.get_account(conn, organization_id)
        if acct is None or not acct["stripe_subscription_id"]:
            return False
        try:
            state = BillingState(acct["billing_state"])
        except ValueError:
            return False
        if state not in PAID_ACCESS_STATES:
            return False
        if acct["stripe_price_id"] != price:
            return False
        if acct["livemode"] is None or bool(acct["livemode"]) != cfg.livemode:
            return False
        period_end = _parse(acct["current_period_end"])
        if period_end is None:
            return False
        if (now or datetime.now(timezone.utc)) > period_end + STALE_PERIOD_GRACE:
            return False
        return True
    except Exception:  # database unavailable, schema missing, ... -> fail closed
        log.warning("billing entitlement check failed closed for organization %s", organization_id)
        return False


def supplier_has_paid_access(conn, organization_id, config=None, *, now=None) -> bool:
    return has_paid_access(conn, organization_id, FOUNDING_SUPPLY_PARTNER, config, now=now)


def contractor_has_pro(conn, organization_id, config=None, *, now=None) -> bool:
    return has_paid_access(conn, organization_id, CONTRACTOR_PRO, config, now=now)


def entitlements_for(conn, organization_id, config=None) -> frozenset[str]:
    cfg = config or load_config()
    return frozenset(p.entitlement for p in PLANS.values() if has_paid_access(conn, organization_id, p, cfg))


def request_priority(conn, organization_id, config=None) -> str:
    """THE integration contract for contractor material requests.

    Returns PRIORITY only for a contractor organization whose Contractor Pro
    subscription is verified paid (contractor_has_pro); every other case —
    free, checkout pending, incomplete, past_due (no grace period), unpaid,
    canceled, supplier organizations, unknown/misconfigured state — is
    STANDARD. The only input is the organization ID, which callers must take
    from the authenticated session; no request field, flag or plan name can
    influence it. A classification only: no response-time or service-level
    commitment of any kind."""
    return PRIORITY if contractor_has_pro(conn, organization_id, config) else STANDARD
