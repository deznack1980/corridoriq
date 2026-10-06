"""Phase 2 retest remediations: throttle-before-verify, fail-safe env, dummy hash."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

from pipeline.auth import mail, passwords, readiness, throttle
from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import SCHEMA_PATH
from pipeline.tests.test_billing import _req
from pipeline.tests.test_contractor_pilot import PW, client, signup
from pipeline.tests.test_phase2_remediation import J, REPO, env  # noqa: F401


def test_pilot_ip_throttle_blocks_correct_password_on_unlocked_account(env):
    throttle.pilot.clear()
    signup(env, "unlocked-ip@example.test", "Unlocked IP Co")
    limit = throttle.PILOT_LIMITS["login_ip"]
    for i in range(limit):
        status, _ = client(env).call("POST", "/api/pilot/auth/login",
                                     {"email": f"spray-ip-{i}@example.test", "password": "wrong-pass"})
        assert status == 401
    assert client(env).call("POST", "/api/pilot/auth/login",
                            {"email": "unlocked-ip@example.test", "password": PW})[0] == 429


def test_pilot_email_throttle_blocks_correct_password(env):
    throttle.pilot.clear()
    signup(env, "throt-email@example.test", "Throt Email Co")
    signup(env, "other-ok@example.test", "Other Ok Co")
    limit = throttle.PILOT_LIMITS["login_email"]
    for _ in range(limit):
        client(env).call("POST", "/api/pilot/auth/login",
                         {"email": "throt-email@example.test", "password": "wrong-pass"})
    assert client(env).call("POST", "/api/pilot/auth/login",
                            {"email": "throt-email@example.test", "password": PW})[0] == 429
    assert client(env).call("POST", "/api/pilot/auth/login",
                            {"email": "other-ok@example.test", "password": PW})[0] == 200


def test_portal_ip_throttle_blocks_correct_password_on_unlocked_account(env):
    throttle.portal.clear()
    port = env["port"]
    conn = env["factory"]()
    org = conn.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    auth.create_user(conn, organization_id=org, email="portal-unlocked@example.test",
                     password="Passw0rd!x", role_names=["admin"], must_change_password=False)
    conn.close()
    limit = throttle.PORTAL_LIMITS["login_ip"]
    for i in range(limit):
        s, _, _ = _req(port, "POST", "/api/auth/login",
                       {"email": f"spray-portal-{i}@example.test", "password": "wrong-pass"}, J)
        assert s == 401
    s, _, raw = _req(port, "POST", "/api/auth/login",
                     {"email": "portal-unlocked@example.test", "password": "Passw0rd!x"}, J)
    assert s == 429 and json.loads(raw)["error"] == "too_many_requests"


def test_existing_and_unknown_login_errors_match(env):
    throttle.pilot.clear()
    signup(env, "exists-login@example.test", "Exists Login Co")
    known = client(env).call("POST", "/api/pilot/auth/login",
                             {"email": "exists-login@example.test", "password": "wrong-pass"})
    missing = client(env).call("POST", "/api/pilot/auth/login",
                               {"email": "no-such-login@example.test", "password": "wrong-pass"})
    assert known == missing == (401, {"error": "invalid_login"})


def test_malformed_login_fields_are_400(env):
    c = client(env)
    assert c.call("POST", "/api/pilot/auth/login",
                  {"email": ["x@example.test"], "password": PW})[0] == 400
    s, _, raw = _req(env["port"], "POST", "/api/auth/login",
                     {"email": "x@example.test", "password": ["secret"]}, J)
    assert s == 400 and json.loads(raw)["error"] == "invalid_request"


def test_environment_model_fail_closed(monkeypatch):
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", False)
    monkeypatch.setenv("CORRIDORIQ_ENV", "development")
    assert readiness.environment_name() == "development"
    assert readiness.runtime_problems() == []
    monkeypatch.setenv("CORRIDORIQ_ENV", "test")
    assert readiness.environment_name() == "test"
    assert readiness.runtime_problems() == []
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", True)
    monkeypatch.setenv("CORRIDORIQ_TRUSTED_PROXY_SECRET", "x" * 32)
    monkeypatch.setenv(mail.ENV_PUBLIC_BASE_URL, "https://app.corridoriq.pro")
    monkeypatch.delenv(mail.ENV_PROVIDER, raising=False)
    assert readiness.environment_name() == "production"
    assert "production_mail_sink" in readiness.runtime_problems()
    monkeypatch.delenv("CORRIDORIQ_ENV")
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", False)
    assert readiness.environment_name() == "unset"
    assert "environment_unset" in readiness.runtime_problems()
    with pytest.raises(SystemExit):
        readiness.refuse_unsafe_production()
    for bad in ("prod", "live", "production ", " production", "PRODUCTION", "prodution"):
        monkeypatch.setenv("CORRIDORIQ_ENV", bad)
        assert readiness.environment_name() == "unsupported"
        assert "environment_unsupported" in readiness.runtime_problems()
        with pytest.raises(SystemExit):
            readiness.refuse_unsafe_production()
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", True)
    monkeypatch.delenv("CORRIDORIQ_TRUSTED_PROXY_SECRET", raising=False)
    monkeypatch.setenv(mail.ENV_PUBLIC_BASE_URL, "http://app.corridoriq.pro")
    problems = readiness.runtime_problems()
    assert "trusted_proxy_secret_missing" in problems
    assert "production_mail_sink" in problems
    assert "public_base_url_not_https" in problems
    src = (REPO / "scripts" / "start-production-server.ps1").read_text(encoding="utf-8")
    assert 'CORRIDORIQ_ENV = "production"' in src
    hq = (REPO / "CorridorIQHQ.bat").read_text(encoding="utf-8")
    assert "CORRIDORIQ_ENV=development" in hq


def test_unknown_user_performs_real_password_verification(monkeypatch):
    dummy = passwords.dummy_password_hash()
    assert dummy.startswith(("$argon2", "scrypt$"))
    assert dummy == passwords.dummy_password_hash()
    assert passwords.verify_password("not-the-dummy-secret!!", dummy) is False
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(conn)
    org = conn.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    auth.create_user(conn, organization_id=org, email="known-timing@example.test",
                     password=PW, role_names=["admin"], must_change_password=False)
    seen = []
    real = passwords.verify_password

    def wrapped(pw, stored):
        seen.append(stored)
        return real(pw, stored)

    monkeypatch.setattr(passwords, "verify_password", wrapped)
    with pytest.raises(auth.AuthError):
        auth.login(conn, "missing-timing@example.test", "wrong-pass")
    with pytest.raises(auth.AuthError):
        auth.login(conn, "known-timing@example.test", "wrong-pass")
    assert seen[0] == dummy
    assert seen[1] != dummy
    assert seen[0] == dummy
    assert seen[1] != dummy
    samples = []
    for _ in range(3):
        t0 = time.perf_counter()
        with pytest.raises(auth.AuthError):
            auth.login(conn, "missing-timing@example.test", "wrong-pass")
        unknown = time.perf_counter() - t0
        t0 = time.perf_counter()
        with pytest.raises(auth.AuthError):
            auth.login(conn, "known-timing@example.test", "wrong-pass")
        wrong = time.perf_counter() - t0
        samples.append((unknown, wrong))
    # Broad bound only: unknown-user work is the same KDF, not a cheap throw.
    ratios = [u / w for u, w in samples if w > 0]
    assert ratios and min(ratios) > 0.2 and max(ratios) < 5.0
    conn.close()


def test_pilot_logout_cookie_is_secure_in_production(env, monkeypatch):
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", True)
    c, _ = signup(env, "logout-secure@example.test", "Logout Secure Co")
    status, _ = c.call("POST", "/api/pilot/auth/logout", {})
    assert status == 200
    assert "Secure" in c.last_pilot_cookie
    assert "Max-Age=0" in c.last_pilot_cookie
