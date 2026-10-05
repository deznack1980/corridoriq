"""Internal billing state and its deliberate mapping from Stripe statuses.

Every Stripe subscription status is listed explicitly. Anything not listed
(a status Stripe adds later) maps to UNKNOWN, which never grants access.
"""

from __future__ import annotations

from enum import Enum


class BillingState(str, Enum):
    NONE = "none"                          # no subscription, no open Checkout
    CHECKOUT_PENDING = "checkout_pending"  # Checkout Session open, nothing confirmed
    INCOMPLETE = "incomplete"              # first payment not yet confirmed
    INCOMPLETE_EXPIRED = "incomplete_expired"
    TRIALING = "trialing"
    ACTIVE = "active"
    PAST_DUE = "past_due"
    UNPAID = "unpaid"
    PAUSED = "paused"
    CANCELED = "canceled"
    UNKNOWN = "unknown"


STRIPE_STATUS_MAP: dict[str, BillingState] = {
    "incomplete": BillingState.INCOMPLETE,
    "incomplete_expired": BillingState.INCOMPLETE_EXPIRED,
    "trialing": BillingState.TRIALING,
    "active": BillingState.ACTIVE,
    "past_due": BillingState.PAST_DUE,
    "unpaid": BillingState.UNPAID,
    "paused": BillingState.PAUSED,
    "canceled": BillingState.CANCELED,
}

# States in which a subscription is still live in Stripe (a second Checkout
# would create a duplicate subscription, so it is refused).
LIVE_SUBSCRIPTION_STATES = frozenset({
    BillingState.INCOMPLETE, BillingState.TRIALING, BillingState.ACTIVE,
    BillingState.PAST_DUE, BillingState.UNPAID, BillingState.PAUSED,
    BillingState.UNKNOWN,
})

# The only states that can ever grant paid access (see entitlements.py).
PAID_ACCESS_STATES = frozenset({BillingState.ACTIVE, BillingState.TRIALING})


def map_stripe_status(status) -> BillingState:
    return STRIPE_STATUS_MAP.get(str(status or ""), BillingState.UNKNOWN)
