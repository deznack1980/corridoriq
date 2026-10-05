"""The two CorridorIQ subscription plans.

An organization's account type (organizations.account_type, set by CorridorIQ,
never by the browser) determines the single plan it may buy:

    supplier    -> Founding Supply Partner  -> supplier_intelligence
    contractor  -> Contractor Pro           -> contractor_pro

Stripe Price IDs are configuration (environment), not code. The amounts below
are the commercial expectation that `verify-price` checks each configured
Stripe Price against; billing logic itself only ever compares price IDs.
"""

from __future__ import annotations

from dataclasses import dataclass

SUPPLIER = "supplier"
CONTRACTOR = "contractor"
ACCOUNT_TYPES = frozenset({SUPPLIER, CONTRACTOR})

SUPPLIER_INTELLIGENCE = "supplier_intelligence"
CONTRACTOR_PRO_ENTITLEMENT = "contractor_pro"


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    short_name: str
    audience: str            # the only account type that may buy this plan
    entitlement: str         # granted only while Stripe state is paid
    display_price: str       # presentation only
    unit_amount: int         # expected Stripe unit_amount (cents), checked by verify-price
    price_env: str
    lookup_env: str
    tagline: str
    currency: str = "usd"
    interval: str = "month"


FOUNDING_SUPPLY_PARTNER = Plan(
    key="founding_supply_partner",
    name="CorridorIQ Founding Supply Partner",
    short_name="Founding Supply Partner",
    audience=SUPPLIER,
    entitlement=SUPPLIER_INTELLIGENCE,
    display_price="$750/month",
    unit_amount=75000,
    price_env="STRIPE_FOUNDING_SUPPLY_PRICE_ID",
    lookup_env="STRIPE_FOUNDING_SUPPLY_PRICE_LOOKUP_KEY",
    tagline="",  # supplier billing page copy unchanged
)

CONTRACTOR_PRO = Plan(
    key="contractor_pro",
    name="CorridorIQ Contractor Pro",
    short_name="Contractor Pro",
    audience=CONTRACTOR,
    entitlement=CONTRACTOR_PRO_ENTITLEMENT,
    display_price="$99/month",
    unit_amount=9900,
    price_env="STRIPE_CONTRACTOR_PRO_PRICE_ID",
    lookup_env="STRIPE_CONTRACTOR_PRO_PRICE_LOOKUP_KEY",
    tagline=("Priority access to CorridorIQ's contractor workflow and premium capabilities "
             "as they become available. Month-to-month; cancel anytime."),
)

PLANS: dict[str, Plan] = {p.key: p for p in (FOUNDING_SUPPLY_PARTNER, CONTRACTOR_PRO)}
_BY_AUDIENCE: dict[str, Plan] = {p.audience: p for p in PLANS.values()}


def plan_for_account_type(account_type) -> Plan | None:
    """The one plan an account type is eligible for, or None (fail closed)."""
    return _BY_AUDIENCE.get(account_type) if account_type in ACCOUNT_TYPES else None
