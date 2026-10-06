"""Runtime environment and production identity/readiness checks.

These do not configure Cloudflare, email providers, or billing. They only
refuse a process that is missing required local invariants.

Supported CORRIDORIQ_ENV values are exact: development, test, production.
Unknown, malformed, or unset values cannot silently run as a public server.
"""

from __future__ import annotations

import os

from pipeline.api.client_address import SECRET_ENV
from pipeline.auth import mail
from pipeline.config import settings

ENV = "CORRIDORIQ_ENV"
SUPPORTED = frozenset({"development", "test", "production"})
UNSET = "unset"
UNSUPPORTED = "unsupported"


def environment_value() -> str | None:
    """Raw CORRIDORIQ_ENV, or None when the variable is absent."""
    if ENV not in os.environ:
        return None
    return os.environ.get(ENV)


def environment_name() -> str:
    """Stable, secret-free label for health/ops. Never the raw unknown value."""
    raw = environment_value()
    if raw is None or raw == "":
        return UNSET
    if raw in SUPPORTED:
        return raw
    return UNSUPPORTED


def is_production() -> bool:
    return environment_value() == "production" or bool(settings.AUTH_PRODUCTION)


def environment_problems() -> list[str]:
    raw = environment_value()
    if raw is None or raw == "":
        return ["environment_unset"]
    if raw not in SUPPORTED:
        return ["environment_unsupported"]
    return []


def production_problems() -> list[str]:
    """Stable, secret-free problem codes. Empty when not in production."""
    if not is_production():
        return []
    problems = []
    if not (os.environ.get(SECRET_ENV) or "").strip():
        problems.append("trusted_proxy_secret_missing")
    provider = mail.provider_name()
    if provider == mail.SINK:
        problems.append("production_mail_sink")
    else:
        problems.append("production_mail_provider_unsupported")
    base = (os.environ.get(mail.ENV_PUBLIC_BASE_URL) or "").strip()
    if not base.lower().startswith("https://"):
        problems.append("public_base_url_not_https")
    return problems


def runtime_problems() -> list[str]:
    return environment_problems() + production_problems()


def production_status() -> dict:
    problems = runtime_problems()
    return {
        "ready": not problems,
        "problems": problems,
        "environment": environment_name(),
    }


def refuse_unsafe_production() -> None:
    """Abort process startup when the environment is unknown or production is unsafe."""
    problems = runtime_problems()
    if problems:
        raise SystemExit("server readiness failed: " + ", ".join(problems))
