"""Resolve the client address for rate limits and audit logs.

Default: use the TCP peer only. Forwarded headers are ignored unless
CORRIDORIQ_TRUST_PROXY is enabled AND the peer is in the trusted proxy
list. That prevents arbitrary clients from spoofing X-Forwarded-For.

Do not enable TRUST_PROXY until the origin is unreachable except through
the intended proxy. Prefer an explicit /32 hop over the `private` alias.
See docs/operations/proxy_trust.md.

Cloudflare's CF-Connecting-IP is preferred over X-Forwarded-For only
when that edge is actually in the HTTP path.
"""

from __future__ import annotations

import ipaddress

from pipeline.config import settings

_LOOPBACK = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)
_PRIVATE = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def parse_ip(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip().strip("[]")
    if text.count(":") == 1 and "." in text:
        text = text.rsplit(":", 1)[0]
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _networks():
    configured = (getattr(settings, "TRUSTED_PROXIES", "") or "").strip()
    tokens = [part.strip() for part in configured.split(",") if part.strip()]
    if not tokens:
        tokens = ["loopback"]
    nets = []
    for token in tokens:
        key = token.lower()
        if key in ("loopback", "localhost"):
            nets.extend(_LOOPBACK)
        elif key == "private":
            nets.extend(_PRIVATE)
        else:
            try:
                nets.append(ipaddress.ip_network(token, strict=False))
            except ValueError:
                ip = parse_ip(token)
                if ip:
                    addr = ipaddress.ip_address(ip)
                    nets.append(ipaddress.ip_network(f"{ip}/{addr.max_prefixlen}"))
    return nets


def is_trusted_peer(peer: str | None) -> bool:
    if not bool(getattr(settings, "TRUST_PROXY", False)):
        return False
    ip = parse_ip(peer)
    if not ip:
        return False
    addr = ipaddress.ip_address(ip)
    return any(addr in net for net in _networks())


def _header(headers, name: str) -> str:
    if headers is None:
        return ""
    getter = getattr(headers, "get", None)
    if callable(getter):
        return getter(name) or getter(name.lower()) or ""
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return value or ""
    return ""


def resolve(peer: str | None, headers=None) -> str | None:
    """Return the address to key rate limits on.

    Untrusted peers: the socket address only (spoofed headers ignored).
    Trusted peers: CF-Connecting-IP, then the right-most untrusted hop
    in X-Forwarded-For, then X-Real-IP, then the peer.
    """
    peer_ip = parse_ip(peer)
    if not is_trusted_peer(peer_ip):
        return peer_ip

    cf = parse_ip(_header(headers, "CF-Connecting-IP"))
    if cf:
        return cf

    xff = _header(headers, "X-Forwarded-For")
    if xff:
        hops = [parse_ip(part) for part in xff.split(",")]
        hops = [hop for hop in hops if hop]
        for hop in reversed(hops):
            if not is_trusted_peer(hop):
                return hop
        if hops:
            return hops[0]

    real = parse_ip(_header(headers, "X-Real-IP"))
    if real:
        return real
    return peer_ip
