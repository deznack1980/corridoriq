"""Operator checks for billing configuration. Never prints secret values.

    python -m pipeline.billing check-config                 # offline: env validation only
    python -m pipeline.billing verify-price                 # read-only Stripe GET of every configured plan price
    python -m pipeline.billing verify-price --plan contractor_pro
    python -m pipeline.billing verify-price --allow-live    # same, permitted against live keys
    python -m pipeline.billing health                       # local billing health report (read-only DB)
    python -m pipeline.billing incidents                    # wrong-plan / duplicate / rejected incidents

verify-price creates nothing and charges nothing; for each configured plan it
confirms the Stripe Price exists in the configured mode, is active, recurring
monthly, in USD, for the plan's expected amount, and (when the plan's lookup-key
variable is set) carries that lookup key.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from pipeline.billing.config import load_config
from pipeline.billing.gateway import BillingGatewayError, default_gateway
from pipeline.billing.plans import PLANS


def check_config(cfg) -> int:
    print(f"billing enabled : {cfg.enabled}")
    print(f"stripe mode     : {cfg.mode or 'unknown'}")
    print(f"production env  : {cfg.production}")
    for plan in PLANS.values():
        print(f"{plan.key:24s}: price {cfg.price_id_for(plan) or '(unset)'}  lookup "
              f"{cfg.lookup_key_for(plan) or '(unset)'}  [{plan.audience}, {plan.display_price}]")
    print(f"public base url : {cfg.public_base_url or '(unset)'}")
    print(f"webhook secret  : {'set' if cfg.webhook_secret else 'unset'}")
    problems = cfg.webhook_problems() + [p for p in cfg.problems() if p not in cfg.webhook_problems()]
    for p in problems:
        print(f"  PROBLEM: {p}")
    print("OK" if not problems else f"{len(problems)} problem(s)")
    return 0 if not problems else 1


def _verify_one(cfg, plan, gateway) -> bool:
    price_id = cfg.price_id_for(plan)
    try:
        price = gateway.retrieve_price(price_id)
    except BillingGatewayError as exc:
        print(f"{plan.key}: could not retrieve the price: {exc}")
        return False
    recurring = price.get("recurring") or {}
    product = price.get("product")
    checks = {
        "mode matches keys": bool(price.get("livemode")) == cfg.livemode,
        "price is active": bool(price.get("active")),
        "type recurring": price.get("type") == "recurring",
        "recurring monthly": recurring.get("interval") == plan.interval and recurring.get("interval_count", 1) == 1,
        f"currency {plan.currency.upper()}": price.get("currency") == plan.currency,
        f"amount {plan.unit_amount}": price.get("unit_amount") == plan.unit_amount,
    }
    if cfg.lookup_key_for(plan):
        checks["lookup key matches"] = price.get("lookup_key") == cfg.lookup_key_for(plan)
    print(f"{plan.key}: price {price.get('id')}  product "
          f"{product if isinstance(product, str) else (product or {}).get('id')}  "
          f"{price.get('unit_amount')} {str(price.get('currency', '')).upper()} / {recurring.get('interval')}")
    for name, ok in checks.items():
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return all(checks.values())


def verify_price(cfg, allow_live: bool, gateway=None, plan_key: str | None = None) -> int:
    if cfg.mode is None:
        print("STRIPE_SECRET_KEY is missing or invalid.")
        return 1
    if cfg.livemode and not allow_live:
        print("Live keys configured: re-run with --allow-live to perform this read-only check.")
        return 1
    plans = [PLANS[plan_key]] if plan_key else [p for p in PLANS.values() if cfg.price_id_for(p)]
    if not plans or any(not cfg.price_id_for(p) for p in plans):
        print("No price configured for the requested plan(s).")
        return 1
    gw = gateway or default_gateway(cfg)
    results = [_verify_one(cfg, p, gw) for p in plans]
    return 0 if all(results) else 1


def _read_only_db():
    from pipeline.config.settings import DB_PATH
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check-config")
    vp = sub.add_parser("verify-price")
    vp.add_argument("--allow-live", action="store_true")
    vp.add_argument("--plan", choices=sorted(PLANS))
    sub.add_parser("health")
    sub.add_parser("incidents")
    args = parser.parse_args(argv)
    cfg = load_config()
    if args.cmd == "check-config":
        return check_config(cfg)
    if args.cmd in ("health", "incidents"):
        from pipeline.billing.health import billing_health, billing_incidents
        conn = _read_only_db()
        try:
            data = billing_health(conn, cfg) if args.cmd == "health" else billing_incidents(conn)
        finally:
            conn.close()
        print(json.dumps(data, indent=2))
        return 0
    return verify_price(cfg, args.allow_live, plan_key=args.plan)


if __name__ == "__main__":
    sys.exit(main())
