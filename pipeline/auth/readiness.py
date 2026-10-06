"""Production identity/runtime readiness checks.

These do not configure Cloudflare, email providers, or billing. They only
refuse a production process that is missing required local invariants.
"""

from __future__ import annotations

import os

from pipeline.api.client_address import SECRET_ENV
from pipeline.auth import mail
from pipeline.config import settings

ENV = "CORRIDORIQ_ENV"


def is_production() -> bool:
    return bool(settings.AUTH_PRODUCTION) or (os.environ.get(ENV) or "").strip().lower() == "production"


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
    if not settings.AUTH_PRODUCTION:
        problems.append("session_cookies_not_secure")
    return problems


def production_status() -> dict:
    problems = production_problems()
    return {"ready": not problems, "problems": problems}


def refuse_unsafe_production() -> None:
    """Abort process startup when production is missing required identity config."""
    problems = production_problems()
    if problems:
        raise SystemExit("production readiness failed: " + ", ".join(problems))
