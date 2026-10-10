"""Client address used for rate limits and audit.

Two mutually independent, fail-closed trust modes resolve the client IP; both
require the high-entropy shared proxy secret and exactly one valid
CF-Connecting-IP, and neither ever consults X-Forwarded-For:

* Legacy tunnel mode (default): Cloudflare's tunnel reaches this process on the
  loopback interface and adds CF-Connecting-IP. The header is honored only when
  the TCP peer is loopback AND the request presents the matching proxy secret.

* Hosted proxy mode (opt-in via ``CORRIDORIQ_HOSTED_PROXY=1``): Cloudflare ->
  Railway edge -> this process, where the TCP peer is the platform's internal
  proxy (not loopback). The loopback check is dropped, but the matching proxy
  secret is still required. This mode is enabled only by its explicit flag,
  never inferred from the presence of RAILWAY_* or any other variable.

In every other case — no secret configured, no/empty presented secret, wrong
secret, missing/malformed/multiple CF-Connecting-IP, or a direct connection
without the secret — the resolver falls back to the real TCP peer. It never
fails open, so no client-controlled header can spoof the client IP.

A third, independent mode serves Railway's edge (opt-in via
``CORRIDORIQ_CLIENT_IP_SOURCE=railway``). Railway documents that its edge sets
``X-Real-IP`` to the client's address, and overwrites any client-supplied copy.
It publishes no stable proxy range, so no CIDR is hard-coded. Instead, the
header is honored only when the TCP peer is a non-public address (the
connection arrived over Railway's internal network) and the header holds
exactly one public address. A public peer, which would mean the edge was
bypassed, falls back to the peer. ``X-Forwarded-For`` is still never consulted.

When no mode attributes the request, the peer may be a platform-internal hop
shared by every visitor. :func:`is_shared_address` lets the throttle treat
such keys as platform-wide rather than per visitor.
"""

from __future__ import annotations

import hmac
import ipaddress
import os

CONNECTING_IP = "CF-Connecting-IP"
PROXY_SECRET = "X-CorridorIQ-Proxy-Secret"
SECRET_ENV = "CORRIDORIQ_TRUSTED_PROXY_SECRET"
HOSTED_ENV = "CORRIDORIQ_HOSTED_PROXY"
REAL_IP = "X-Real-IP"
SOURCE_ENV = "CORRIDORIQ_CLIENT_IP_SOURCE"
RAILWAY = "railway"


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


def hosted_proxy_enabled(value: str | None = None) -> bool:
    """True only when hosted proxy mode is explicitly enabled (exact "1")."""
    raw = os.environ.get(HOSTED_ENV, "") if value is None else value
    return raw == "1"


def client_ip_source(value: str | None = None) -> str:
    """``"railway"`` only when explicitly configured; anything else is ``"peer"``."""
    raw = os.environ.get(SOURCE_ENV, "") if value is None else value
    return RAILWAY if raw.strip().lower() == RAILWAY else "peer"


def _internal(value: str | None) -> bool:
    """Non-public and non-loopback: a platform network hop, not a visitor."""
    try:
        addr = ipaddress.ip_address(value)
    except (TypeError, ValueError):
        return False
    return not addr.is_global and not addr.is_loopback


def _public(value: str | None) -> bool:
    try:
        return ipaddress.ip_address(value).is_global
    except (TypeError, ValueError):
        return False


def is_shared_address(ip: str | None) -> bool:
    """True when ``ip`` is a platform-internal hop shared by many visitors."""
    return _internal(ip)


def resolve_client_ip(peer: str | None, headers, *, secret: str | None = None,
                      hosted: bool | None = None, source: str | None = None) -> str | None:
    """Return the address to record. Untrusted headers never replace the peer.

    ``hosted`` and ``source`` override the ``CORRIDORIQ_HOSTED_PROXY`` and
    ``CORRIDORIQ_CLIENT_IP_SOURCE`` env settings for tests.
    """
    peer_ip = _single_ip(peer) or (peer or None)
    configured = os.environ.get(SECRET_ENV, "") if secret is None else secret
    presented = ""
    connecting = None
    if headers is not None:
        presented = headers.get(PROXY_SECRET) or ""
        connecting = _single_ip(headers.get(CONNECTING_IP))
    hosted_on = hosted_proxy_enabled() if hosted is None else bool(hosted)
    # Both modes require: a configured secret, a presented secret matching in
    # constant time, and exactly one valid CF-Connecting-IP. Legacy mode also
    # requires a loopback TCP peer; hosted mode (explicit opt-in) drops only
    # that check. X-Forwarded-For is never consulted.
    if (
        configured
        and presented
        and connecting
        and hmac.compare_digest(presented, configured)
        and (hosted_on or (peer_ip and _loopback(peer_ip)))
    ):
        return connecting
    # Railway edge mode (explicit opt-in): the edge overwrites X-Real-IP, and
    # only connections relayed over the platform network reach us from a
    # non-public peer. Require exactly one public address in the header.
    src = client_ip_source() if source is None else client_ip_source(source)
    if src == RAILWAY and headers is not None and _internal(peer_ip):
        real = _single_ip(headers.get(REAL_IP))
        if real and _public(real):
            return real
    return peer_ip
