"""Stripe subscription billing for supplier organizations.

    Supplier organization -> Stripe Customer -> Checkout Session -> Subscription
    -> signed webhook -> billing_accounts -> entitlement

Stripe is authoritative for payment state; CorridorIQ is authoritative for
application access. Access is derived only from subscription state that the
server fetched from Stripe after a signature-verified webhook, never from the
browser returning from Checkout.

The whole feature is off unless CORRIDORIQ_BILLING_ENABLED=1 (see config.py);
while off, every /api/billing route and the billing page answer 404.
"""
