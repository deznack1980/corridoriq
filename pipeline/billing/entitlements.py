"""Application entitlements derived from billing state.

    BILLING STATE (what Stripe says)  !=  ENTITLEMENT (what CorridorIQ grants)

This module is the only place that turns billing state into access. Callers
ask `supplier_has_paid_access(conn, organization_id)` or
`entitlements_for(...)`; nothing else in CorridorIQ should compare Stripe
statuses. Every doubt resolves to "no paid access".

Not yet wired into any existing access check: current production access is
unchanged until that is separately approved.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from pipeline.billing import store
from pipeline.billing.config import load_config
from pipeline.billing.states import PAID_ACCESS_STATES, BillingState

log = logging.getLogger(__name__)

SUPPLIER_INTELLIGENCE = "supplier_intelligence"
PLAN_ENTITLEMENTS = frozenset({SUPPLIER_INTELLIGENCE})

# A renewal webhook is normally received within minutes of the period end.
# If none has arrived this long after the recorded period end, stop granting
# access rather than trust state that may be stale.
STALE_PERIOD_GRACE = timedelta(days=3)


def _parse(ts):
    try:
        return datetime.fromisoformat(ts) if ts else None
    except (TypeError, ValueError):
        return None


def supplier_has_paid_access(conn, organization_id, config=None, *, now=None) -> bool:
    try:
        cfg = config or load_config()
        if not cfg.price_id.startswith("price_") or cfg.mode is None:
            return False
        org = conn.execute("SELECT is_active FROM organizations WHERE id=?", (organization_id,)).fetchone()
        if org is None or not org["is_active"]:
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
        if acct["stripe_price_id"] != cfg.price_id:
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


def entitlements_for(conn, organization_id, config=None) -> frozenset[str]:
    return PLAN_ENTITLEMENTS if supplier_has_paid_access(conn, organization_id, config) else frozenset()
