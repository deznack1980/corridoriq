"""Trusted-proxy / client-IP resolution: legacy tunnel + hosted (Railway) modes.

Both modes are fail-closed: they require the shared proxy secret and exactly one
valid CF-Connecting-IP, and never consult X-Forwarded-For. Hosted mode only drops
the loopback-peer requirement, and only when explicitly enabled.
"""

from __future__ import annotations

import pytest

from pipeline.api.client_address import (
    CONNECTING_IP, HOSTED_ENV, PROXY_SECRET, SECRET_ENV,
    hosted_proxy_enabled, resolve_client_ip,
)

SECRET = "test-proxy-secret-value-not-real-0123456789"
CF4 = "203.0.113.40"
CF6 = "2001:db8::1234"
LOOPBACK = "127.0.0.1"
EDGE = "100.64.0.7"          # non-loopback (platform internal proxy)


def H(**kw):
    """Build a case-correct header dict for the resolver."""
    h = {}
    if "secret" in kw:
        h[PROXY_SECRET] = kw["secret"]
    if "cf" in kw:
        h[CONNECTING_IP] = kw["cf"]
    if "xff" in kw:
        h["X-Forwarded-For"] = kw["xff"]
    return h


# ---- legacy tunnel mode (hosted disabled) -----------------------------------

def test_legacy_loopback_correct_secret_valid_cf_returns_cf():
    assert resolve_client_ip(LOOPBACK, H(secret=SECRET, cf=CF4), secret=SECRET, hosted=False) == CF4


def test_legacy_nonloopback_correct_secret_returns_peer():
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=CF4), secret=SECRET, hosted=False) == EDGE


def test_legacy_loopback_wrong_secret_returns_peer():
    assert resolve_client_ip(LOOPBACK, H(secret="wrong", cf=CF4), secret=SECRET, hosted=False) == LOOPBACK


def test_legacy_loopback_no_secret_configured_returns_peer():
    assert resolve_client_ip(LOOPBACK, H(secret=SECRET, cf=CF4), secret="", hosted=False) == LOOPBACK


# ---- hosted proxy mode (explicit opt-in) ------------------------------------

def test_hosted_correct_secret_valid_cf_returns_cf():
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=CF4), secret=SECRET, hosted=True) == CF4


def test_hosted_wrong_secret_returns_peer():
    assert resolve_client_ip(EDGE, H(secret="nope", cf=CF4), secret=SECRET, hosted=True) == EDGE


def test_hosted_missing_presented_secret_returns_peer():
    assert resolve_client_ip(EDGE, H(cf=CF4), secret=SECRET, hosted=True) == EDGE


def test_hosted_empty_configured_secret_returns_peer():
    # Even with a presented secret, an unset server secret must never trust the header.
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=CF4), secret="", hosted=True) == EDGE


@pytest.mark.parametrize("bad_cf", ["203.0.113.40, 70.1.2.3", "203.0.113.40 70.1.2.3",
                                    "not-an-ip", "", "999.999.999.999", "203.0.113.0/24"])
def test_hosted_malformed_or_multiple_cf_returns_peer(bad_cf):
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=bad_cf), secret=SECRET, hosted=True) == EDGE


def test_hosted_spoofed_xff_only_returns_peer():
    # X-Forwarded-For is never consulted, even with the correct secret and no CF header.
    assert resolve_client_ip(EDGE, {PROXY_SECRET: SECRET, "X-Forwarded-For": "1.2.3.4"},
                             secret=SECRET, hosted=True) == EDGE


def test_hosted_xff_cannot_override_even_with_secret_and_cf():
    # With a valid CF header the CF IP wins; XFF is ignored entirely.
    assert resolve_client_ip(EDGE, {PROXY_SECRET: SECRET, CONNECTING_IP: CF4, "X-Forwarded-For": "1.2.3.4"},
                             secret=SECRET, hosted=True) == CF4


def test_hosted_disabled_nonloopback_returns_peer():
    # Hosted off + non-loopback peer: even correct secret + valid CF must not be trusted.
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=CF4), secret=SECRET, hosted=False) == EDGE


# ---- IPv4 / IPv6 ------------------------------------------------------------

def test_hosted_ipv6_cf_returns_cf():
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=CF6), secret=SECRET, hosted=True) == CF6


def test_ipv6_loopback_recognized_in_legacy_mode():
    # "::1" is loopback, so legacy mode trusts the CF header (here an IPv6 CF IP).
    assert resolve_client_ip("::1", H(secret=SECRET, cf=CF6), secret=SECRET, hosted=False) == CF6


# ---- no client-controlled header can bypass the secret ----------------------

def test_no_header_bypass_without_secret():
    # A direct client setting CF/XFF but no (or wrong) secret never changes the IP.
    for headers in (
        {CONNECTING_IP: CF4},
        {CONNECTING_IP: CF4, "X-Forwarded-For": CF4},
        {PROXY_SECRET: "", CONNECTING_IP: CF4},
        {PROXY_SECRET: "guess", CONNECTING_IP: CF4},
    ):
        assert resolve_client_ip(EDGE, headers, secret=SECRET, hosted=True) == EDGE
        assert resolve_client_ip(LOOPBACK, headers, secret=SECRET, hosted=False) == LOOPBACK


def test_no_headers_at_all_returns_peer():
    assert resolve_client_ip(EDGE, None, secret=SECRET, hosted=True) == EDGE
    assert resolve_client_ip(LOOPBACK, None, secret=SECRET, hosted=False) == LOOPBACK


# ---- env flag parsing (narrow, exact "1") -----------------------------------

def test_hosted_flag_requires_exact_1(monkeypatch):
    for val, expected in [("1", True), ("0", False), ("true", False), ("yes", False),
                          ("", False), (" 1", False), ("1 ", False)]:
        monkeypatch.setenv(HOSTED_ENV, val)
        assert hosted_proxy_enabled() is expected, val
    monkeypatch.delenv(HOSTED_ENV, raising=False)
    assert hosted_proxy_enabled() is False


def test_not_inferred_from_railway_vars(monkeypatch):
    monkeypatch.delenv(HOSTED_ENV, raising=False)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT", "production")
    monkeypatch.setenv("RAILWAY_SERVICE_ID", "abc")
    assert hosted_proxy_enabled() is False
    # And the resolver, driven by the env flag, stays in legacy mode (peer kept).
    monkeypatch.setenv(SECRET_ENV, SECRET)
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=CF4)) == EDGE


def test_env_driven_hosted_mode(monkeypatch):
    monkeypatch.setenv(SECRET_ENV, SECRET)
    monkeypatch.setenv(HOSTED_ENV, "1")
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=CF4)) == CF4
    monkeypatch.setenv(HOSTED_ENV, "0")
    assert resolve_client_ip(EDGE, H(secret=SECRET, cf=CF4)) == EDGE


# ---- bind HOST/PORT precedence (server.py) -----------------------------------

import subprocess, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _server_bind(env_overrides):
    import os
    env = {k: v for k, v in os.environ.items() if k not in ("CORRIDORIQ_PORT", "PORT", "CORRIDORIQ_HOST")}
    env["CORRIDORIQ_ENV"] = env.get("CORRIDORIQ_ENV", "test")
    env.update(env_overrides)
    code = "import pipeline.api.server as s; print(s.HOST, s.PORT)"
    r = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    host, port = r.stdout.strip().split()
    return host, int(port)


def test_port_precedence_windows_default():
    host, port = _server_bind({})
    assert host == "127.0.0.1" and port == 8780  # Windows defaults preserved


def test_port_precedence_railway_PORT():
    host, port = _server_bind({"PORT": "8080"})
    assert host == "127.0.0.1" and port == 8080  # platform PORT used when CORRIDORIQ_PORT unset


def test_port_precedence_corridoriq_over_PORT():
    host, port = _server_bind({"CORRIDORIQ_PORT": "9001", "PORT": "8080"})
    assert port == 9001  # CORRIDORIQ_PORT wins


def test_host_configurable_for_railway():
    host, port = _server_bind({"CORRIDORIQ_HOST": "0.0.0.0", "PORT": "8080"})
    assert host == "0.0.0.0" and port == 8080
