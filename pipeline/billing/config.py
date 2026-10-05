"""Billing configuration, read from the environment at call time.

Secrets never appear in source control, logs, reprs or API responses. The mode
(test vs live) is derived from the secret key prefix, and the configuration is
rejected outright when it would mix modes or put live keys on a non-production
server, so a misconfigured process fails closed instead of charging anyone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from pipeline.billing.plans import CONTRACTOR_PRO, FOUNDING_SUPPLY_PARTNER, PLANS, Plan

ENV_ENABLED = "CORRIDORIQ_BILLING_ENABLED"
ENV_SECRET_KEY = "STRIPE_SECRET_KEY"
ENV_PUBLISHABLE_KEY = "STRIPE_PUBLISHABLE_KEY"
ENV_WEBHOOK_SECRET = "STRIPE_WEBHOOK_SECRET"
ENV_PRICE_ID = FOUNDING_SUPPLY_PARTNER.price_env
ENV_LOOKUP_KEY = FOUNDING_SUPPLY_PARTNER.lookup_env
ENV_CONTRACTOR_PRICE_ID = CONTRACTOR_PRO.price_env
ENV_CONTRACTOR_LOOKUP_KEY = CONTRACTOR_PRO.lookup_env
ENV_PUBLIC_BASE_URL = "CORRIDORIQ_PUBLIC_BASE_URL"

_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class BillingConfigError(Exception):
    """Billing cannot run with the current configuration (fail closed)."""


def billing_enabled(environ=None) -> bool:
    env = os.environ if environ is None else environ
    return (env.get(ENV_ENABLED) or "").strip() == "1"


def _mode_of(key: str, live_prefixes: tuple[str, ...], test_prefixes: tuple[str, ...]) -> str | None:
    if key.startswith(live_prefixes):
        return "live"
    if key.startswith(test_prefixes):
        return "test"
    return None


@dataclass(frozen=True)
class BillingConfig:
    enabled: bool
    production: bool
    price_id: str                    # Founding Supply Partner (required)
    price_lookup_key: str
    public_base_url: str
    contractor_price_id: str = ""    # Contractor Pro (optional until configured)
    contractor_price_lookup_key: str = ""
    secret_key: str = field(repr=False, default="")
    publishable_key: str = field(repr=False, default="")
    webhook_secret: str = field(repr=False, default="")

    # ---- derived -----------------------------------------------------------
    @property
    def mode(self) -> str | None:
        return _mode_of(self.secret_key, ("sk_live_", "rk_live_"), ("sk_test_", "rk_test_"))

    @property
    def livemode(self) -> bool:
        return self.mode == "live"

    def problems(self) -> list[str]:
        """Human-readable configuration problems. Never includes secret values."""
        out: list[str] = []
        if not self.enabled:
            out.append(f"{ENV_ENABLED} is not 1")
        if not self.secret_key:
            out.append(f"{ENV_SECRET_KEY} is not set")
        elif self.mode is None:
            out.append(f"{ENV_SECRET_KEY} is not a Stripe secret or restricted key")
        if self.mode == "live" and not self.production:
            out.append("live Stripe keys are only accepted with CORRIDORIQ_ENV=production")
        if self.publishable_key:
            pk_mode = _mode_of(self.publishable_key, ("pk_live_",), ("pk_test_",))
            if pk_mode is None:
                out.append(f"{ENV_PUBLISHABLE_KEY} is not a Stripe publishable key")
            elif self.mode and pk_mode != self.mode:
                out.append(f"{ENV_PUBLISHABLE_KEY} and {ENV_SECRET_KEY} are from different modes")
        if not self.price_id.startswith("price_"):
            out.append(f"{ENV_PRICE_ID} is not a Stripe price ID")
        if self.contractor_price_id:
            if not self.contractor_price_id.startswith("price_"):
                out.append(f"{ENV_CONTRACTOR_PRICE_ID} is not a Stripe price ID")
            elif self.contractor_price_id == self.price_id:
                out.append(f"{ENV_CONTRACTOR_PRICE_ID} must differ from {ENV_PRICE_ID}")
        out.extend(self._base_url_problems())
        return out

    def webhook_problems(self) -> list[str]:
        out = [p for p in self.problems() if ENV_PUBLIC_BASE_URL not in p]
        if not self.webhook_secret.startswith("whsec_"):
            out.append(f"{ENV_WEBHOOK_SECRET} is not set")
        return out

    def _base_url_problems(self) -> list[str]:
        url = self.public_base_url
        if not url:
            return [f"{ENV_PUBLIC_BASE_URL} is not set"]
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            return [f"{ENV_PUBLIC_BASE_URL} must be an absolute http(s) origin"]
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username or parts.password:
            return [f"{ENV_PUBLIC_BASE_URL} must be an origin only (no path, query or credentials)"]
        if parts.scheme != "https" and parts.hostname not in _LOCAL_HOSTS:
            return [f"{ENV_PUBLIC_BASE_URL} must use https"]
        if self.mode == "live" and parts.scheme != "https":
            return [f"{ENV_PUBLIC_BASE_URL} must use https with live keys"]
        return []

    # ---- per-plan prices (the server's only source of truth) ----------------
    def price_id_for(self, plan: Plan) -> str:
        return {FOUNDING_SUPPLY_PARTNER.key: self.price_id,
                CONTRACTOR_PRO.key: self.contractor_price_id}.get(plan.key, "")

    def lookup_key_for(self, plan: Plan) -> str:
        return {FOUNDING_SUPPLY_PARTNER.key: self.price_lookup_key,
                CONTRACTOR_PRO.key: self.contractor_price_lookup_key}.get(plan.key, "")

    def plan_for_price(self, price_id) -> Plan | None:
        """Which approved plan a Stripe price belongs to (None = unknown price)."""
        for plan in PLANS.values():
            configured = self.price_id_for(plan)
            if configured and price_id == configured:
                return plan
        return None

    def plan_problems(self, plan: Plan) -> list[str]:
        out = list(self.problems())
        if not self.price_id_for(plan).startswith("price_") and not any(plan.price_env in p for p in out):
            out.append(f"{plan.price_env} is not set")
        return out

    def require_plan_ready(self, plan: Plan) -> "BillingConfig":
        if self.plan_problems(plan):
            raise BillingConfigError("billing is not configured for this plan")
        return self

    def require_ready(self) -> "BillingConfig":
        if self.problems():
            raise BillingConfigError("billing is not configured")
        return self

    def require_webhook_ready(self) -> "BillingConfig":
        if self.webhook_problems():
            raise BillingConfigError("billing webhook is not configured")
        return self

    # ---- fixed, server-built URLs (never from the request) -----------------
    @property
    def origin(self) -> str:
        return self.public_base_url.rstrip("/")

    @property
    def success_url(self) -> str:
        return f"{self.origin}/billing.html?checkout=success&session_id={{CHECKOUT_SESSION_ID}}"

    @property
    def cancel_url(self) -> str:
        return f"{self.origin}/billing.html?checkout=canceled"

    @property
    def portal_return_url(self) -> str:
        return f"{self.origin}/billing.html"


def load_config(environ=None) -> BillingConfig:
    env = os.environ if environ is None else environ
    get = lambda k: (env.get(k) or "").strip()  # noqa: E731
    return BillingConfig(
        enabled=billing_enabled(env),
        production=get("CORRIDORIQ_ENV").lower() == "production",
        price_id=get(ENV_PRICE_ID),
        price_lookup_key=get(ENV_LOOKUP_KEY),
        contractor_price_id=get(ENV_CONTRACTOR_PRICE_ID),
        contractor_price_lookup_key=get(ENV_CONTRACTOR_LOOKUP_KEY),
        public_base_url=get(ENV_PUBLIC_BASE_URL),
        secret_key=get(ENV_SECRET_KEY),
        publishable_key=get(ENV_PUBLISHABLE_KEY),
        webhook_secret=get(ENV_WEBHOOK_SECRET),
    )
