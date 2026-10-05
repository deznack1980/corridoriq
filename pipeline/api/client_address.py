"""Client address used for rate limits and audit.

Cloudflare's tunnel reaches this process on the loopback interface and adds
CF-Connecting-IP. That header is honored only when the TCP peer is loopback
and the request also presents the proxy secret configured for this process.
A direct connection, including one from 127.0.0.1, cannot choose another
address by setting the header. X-Forwarded-For is never consulted.
"""

from __future__ import annotations

import hmac
import ipaddress
import os

CONNECTING_IP = "CF-Connecting-IP"
PROXY_SECRET = "X-CorridorIQ-Proxy-Secret"
SECRET_ENV = "CORRIDORIQ_TRUSTED_PROXY_SECRET"


def _loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _single_ip(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    if not text or any(ch.isspace() or ch == "," for ch in text):
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def resolve_client_ip(peer: str | None, headers, *, secret: str | None = None) -> str | None:
    """Return the address to record. Untrusted headers never replace the peer."""
    peer_ip = _single_ip(peer) or (peer or None)
    configured = os.environ.get(SECRET_ENV, "") if secret is None else secret
    presented = ""
    connecting = None
    if headers is not None:
        presented = headers.get(PROXY_SECRET) or ""
        connecting = _single_ip(headers.get(CONNECTING_IP))
    if (
        configured
        and presented
        and peer_ip
        and _loopback(peer_ip)
        and connecting
        and hmac.compare_digest(presented, configured)
    ):
        return connecting
    return peer_ip
