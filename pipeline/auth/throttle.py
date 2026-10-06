"""In-process sliding-window rate limiter for authentication endpoints.

Keys are chosen by the caller: the resolved client IP (see
``pipeline.api.client_address`` — never a client-supplied header) and, for
email-addressed flows, the normalized email. Responses to throttled requests
are identical in shape to normal ones where enumeration matters; callers
decide whether to return 429 or a generic accepted response.
"""

from __future__ import annotations

import threading
import time


class Throttle:
    def __init__(self, limits: dict[str, int], window_s: int = 600):
        self.limits = dict(limits)
        self.window_s = window_s
        self._hits: dict[tuple[str, str], list[float]] = {}
        self._lock = threading.Lock()

    def _window(self, bucket: str, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get((bucket, key), []) if now - t < self.window_s]
        self._hits[(bucket, key)] = hits
        return hits

    def blocked(self, bucket: str, key: str) -> bool:
        """Side-effect-free: True when this key is already at the limit."""
        limit = self.limits.get(bucket)
        if not limit or key is None:
            return False
        now = time.monotonic()
        with self._lock:
            return len(self._window(bucket, key, now)) >= limit

    def login_blocked(self, ip, email) -> bool:
        """True when IP or email failure buckets already block a login attempt."""
        if self.blocked("login_ip", str(ip or "unknown")):
            return True
        if email and self.blocked("login_email", email):
            return True
        return False

    def hit(self, bucket: str, key: str) -> bool:
        """Record one attempt; return True when the caller must be throttled."""
        limit = self.limits.get(bucket)
        if not limit or key is None:
            return False
        now = time.monotonic()
        with self._lock:
            hits = self._window(bucket, key, now)
            if len(hits) >= limit:
                return True
            hits.append(now)
            self._hits[(bucket, key)] = hits
            return False

    def clear(self) -> None:
        with self._lock:
            self._hits.clear()


# Portal (main application) authentication limits per 10 minutes.
PORTAL_LIMITS = {
    "login_ip": 60,          # failed sign-ins only
    "login_email": 20,       # failed sign-ins only
    "forgot_ip": 10,
    "forgot_email": 5,
    "reset_ip": 20,
    "invite_ip": 20,
    "invite_accept_ip": 20,
}
portal = Throttle(PORTAL_LIMITS)

# Pilot contractor identity limits per 10 minutes.
PILOT_LIMITS = {
    "signup_ip": 10,
    "signup_email": 5,
    "verify_ip": 10,
    "verify_email": 5,
    "login_ip": 30,          # failed sign-ins only
    "login_email": 20,       # failed sign-ins only
    "inspect_ip": 30,
    "forgot_ip": 10,
    "forgot_email": 5,
    "reset_ip": 20,
}
pilot = Throttle(PILOT_LIMITS)
