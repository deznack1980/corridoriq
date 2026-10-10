"""In-process rate limiter for unauthenticated auth writes.

Keyed by action + resolved client address, and again by action + account
identity when an email is supplied. Limits are a safety net on top of
account lockout; they do not replace password hashing or session checks.

Limits are never relaxed for tests or shared proxies. Shared-proxy
lockouts are avoided by resolving the real client IP only from a trusted
peer (see pipeline.auth.client_ip).
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from pipeline.config import settings


class RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[tuple[str, str], list[float]] = defaultdict(list)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()

    def allow(self, action: str, key: str | None, *, limit: int, window_s: float) -> bool:
        identity = (action, key or "unknown")
        now = time.monotonic()
        with self._lock:
            bucket = [t for t in self._hits[identity] if now - t < window_s]
            if len(bucket) >= limit:
                self._hits[identity] = bucket
                return False
            bucket.append(now)
            self._hits[identity] = bucket
            return True


LIMITER = RateLimiter()


def _policy(action: str) -> tuple[int, float]:
    if action == "register":
        return settings.AUTH_REGISTER_PER_HOUR, 3600
    if action == "forgot_password":
        return settings.AUTH_RESET_PER_HOUR, 3600
    if action == "resend_verification":
        return settings.AUTH_RESEND_PER_HOUR, 3600
    if action == "login":
        return settings.AUTH_LOGIN_PER_MINUTE, 60
    if action == "reset_password":
        return settings.AUTH_RESET_PER_HOUR, 3600
    return 20, 3600


def _normalize_identity(identity: str | None) -> str | None:
    if not identity:
        return None
    value = str(identity).strip().lower()
    return value[:254] or None


def check(action: str, ip: str | None, identity: str | None = None) -> None:
    from pipeline.auth.service import AuthError

    limit, window = _policy(action)
    if not LIMITER.allow(action, ip, limit=limit, window_s=window):
        raise AuthError("Too many attempts. Please wait and try again.", status=429)
    account = _normalize_identity(identity)
    if account and not LIMITER.allow(f"{action}:id", account, limit=limit, window_s=window):
        raise AuthError("Too many attempts. Please wait and try again.", status=429)
