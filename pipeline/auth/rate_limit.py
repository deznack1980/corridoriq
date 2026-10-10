"""In-process rate limiter for unauthenticated auth writes.

Keyed by action + client address. Limits are a safety net on top of
account lockout; they do not replace password hashing or session checks.
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


def check(action: str, ip: str | None) -> None:
    from pipeline.auth.service import AuthError

    if action == "register":
        ok = LIMITER.allow(action, ip, limit=settings.AUTH_REGISTER_PER_HOUR, window_s=3600)
    elif action == "forgot_password":
        ok = LIMITER.allow(action, ip, limit=settings.AUTH_RESET_PER_HOUR, window_s=3600)
    elif action == "resend_verification":
        ok = LIMITER.allow(action, ip, limit=settings.AUTH_RESEND_PER_HOUR, window_s=3600)
    elif action == "login":
        ok = LIMITER.allow(action, ip, limit=settings.AUTH_LOGIN_PER_MINUTE, window_s=60)
    else:
        ok = LIMITER.allow(action, ip, limit=20, window_s=3600)
    if not ok:
        raise AuthError("Too many attempts. Please wait and try again.", status=429)
