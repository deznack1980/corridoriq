"""Operator checks for billing configuration. Never prints secret values.

    python -m pipeline.billing check-config            # offline: env validation only
    python -m pipeline.billing verify-price            # read-only Stripe GET of the configured price
    python -m pipeline.billing verify-price --allow-live   # same, permitted against live keys

verify-price creates nothing and charges nothing; it confirms the configured
price exists in the configured mode, is active, recurring monthly, and (when
STRIPE_FOUNDING_SUPPLY_PRICE_LOOKUP_KEY is set) carries that lookup key.
"""

from __future__ import annotations

import argparse
import sys

from pipeline.billing.config import load_config
from pipeline.billing.gateway import BillingGatewayError, default_gateway


def check_config(cfg) -> int:
    print(f"billing enabled : {cfg.enabled}")
    print(f"stripe mode     : {cfg.mode or 'unknown'}")
    print(f"production env  : {cfg.production}")
    print(f"price id        : {cfg.price_id or '(unset)'}")
    print(f"lookup key      : {cfg.price_lookup_key or '(unset)'}")
    print(f"public base url : {cfg.public_base_url or '(unset)'}")
    print(f"webhook secret  : {'set' if cfg.webhook_secret else 'unset'}")
    problems = cfg.webhook_problems() + [p for p in cfg.problems() if p not in cfg.webhook_problems()]
    for p in problems:
        print(f"  PROBLEM: {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


def verify_price(cfg, allow_live: bool, gateway=None) -> int:
    if cfg.mode is None:
        print("STRIPE_SECRET_KEY is missing or invalid.")
        return 1
    if cfg.livemode and not allow_live:
        print("Live keys configured: re-run with --allow-live to perform this read-only check.")
        return 1
    try:
        price = (gateway or default_gateway(cfg)).retrieve_price(cfg.price_id)
    except BillingGatewayError as exc:
        print(f"Could not retrieve the price: {exc}")
        return 1
    recurring = price.get("recurring") or {}
    product = price.get("product")
    checks = {
        "mode matches keys": bool(price.get("livemode")) == cfg.livemode,
        "price is active": bool(price.get("active")),
        "recurring monthly": recurring.get("interval") == "month" and recurring.get("interval_count", 1) == 1,
        "type recurring": price.get("type") == "recurring",
    }
    if cfg.price_lookup_key:
        checks["lookup key matches"] = price.get("lookup_key") == cfg.price_lookup_key
    print(f"price {price.get('id')}  product {product if isinstance(product, str) else (product or {}).get('id')}  "
          f"{price.get('unit_amount')} {str(price.get('currency', '')).upper()} / {recurring.get('interval')}")
    for name, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return 0 if all(checks.values()) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config")
    vp = sub.add_parser("verify-price")
    vp.add_argument("--allow-live", action="store_true")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.cmd == "check-config":
        return check_config(cfg)
    return verify_price(cfg, args.allow_live)


if __name__ == "__main__":
    sys.exit(main())
