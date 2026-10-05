"""CEO system health consumes the billing health contract (pipeline.billing.health).

Every billing status maps deterministically. No test reaches Stripe, and the
CEO-facing record never carries configuration names, Stripe IDs or secrets.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from agents.ceo.analytics import health as ceo_health
from agents.ceo.analytics.health import evaluate_system_health, load_health_record, write_health
from pipeline.billing import health as billing
from pipeline.config.settings import SCHEMA_PATH
from pipeline.tests.test_billing import SK, WHSEC
from pipeline.tests.test_billing_contractor_pro import PRICE_C, ccfg

CHECKED = "2026-10-05T12:00:00+00:00"
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
SECRET_MARKERS = ("password", "token", "cookie", "secret", "api_key", "sk_live")
CUS, SUB = "cus_TESTceohealth01", "sub_TESTceohealth01"


@pytest.fixture(autouse=True)
def _no_real_stripe(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("CEO health must never construct a Stripe client")
    monkeypatch.setattr("pipeline.billing.gateway.StripeGateway.__init__", boom)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _healthy_db(tmp_path: Path, *, billing_tables: bool = True) -> Path:
    path = tmp_path / "health.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("INSERT INTO jurisdictions (slug, name, state, status, connector_type) "
                 "VALUES ('phoenix_az', 'phoenix_az', 'AZ', 'connected', 'arcgis_hub')")
    conn.execute("INSERT INTO pipeline_runs (run_type, status, started_at, completed_at, records_received, created_at) "
                 "VALUES ('morning_refresh', 'succeeded', ?, ?, 0, ?)", (CHECKED, CHECKED, CHECKED))
    conn.execute("INSERT INTO source_health_snapshot (captured_at, jurisdiction_slug, health_state, records_7d, "
                 "days_since_newest_source, newest_source_date) VALUES ('2026-10-05T11:00:00+00:00', 'phoenix_az', "
                 "'healthy', 20, 2, '2026-10-03')")
    org = conn.execute("INSERT INTO organizations (name, slug, is_active, account_type, created_at, updated_at) "
                       "VALUES ('Saguaro Plumbing LLC', 'saguaro', 1, 'contractor', ?, ?)", (CHECKED, CHECKED)).lastrowid
    conn.execute("INSERT INTO billing_accounts (organization_id, stripe_customer_id, stripe_subscription_id, "
                 "stripe_price_id, subscription_status, billing_state, livemode, created_at, updated_at) "
                 "VALUES (?, ?, ?, ?, 'active', 'active', 0, ?, ?)", (org, CUS, SUB, PRICE_C, CHECKED, CHECKED))
    _event(conn, "evt_TESTapplied", "applied", NOW - timedelta(hours=2))
    if not billing_tables:
        conn.execute("DROP TABLE billing_stripe_events")
    conn.commit()
    conn.close()
    return path


def _event(conn, event_id, outcome, received: datetime, org=None):
    conn.execute("INSERT INTO billing_stripe_events (event_id, event_type, livemode, organization_id, outcome, "
                 "received_at, processed_at) VALUES (?, 'customer.subscription.updated', 0, ?, ?, ?, ?)",
                 (event_id, org, outcome, _iso(received), _iso(received)))


def _with(path: Path, fn):
    conn = sqlite3.connect(path)
    fn(conn)
    conn.commit()
    conn.close()


def _eval(path, config):
    return evaluate_system_health(path, checked_at=CHECKED, billing_config=config)


def _billing_check(h):
    return next(item for item in h["checks"] if item["id"] == "billing")


def _assert_clean(h):
    blob = json.dumps(h).lower()
    for value in (CUS, SUB, PRICE_C, SK, WHSEC, "cus_", "sub_", "price_", "whsec_", "sk_test", "rk_"):
        assert value.lower() not in blob, value
    assert not any(marker in blob for marker in SECRET_MARKERS)
    assert h["execute"] is False
    assert all(i["automatic_remediation_permitted"] is False for i in h["incidents"])


# ------------------------------------------------------- status mapping -----

@pytest.mark.parametrize("contract_status, enabled, ceo_status, critical, overall", [
    ("NOT_ENABLED", False, "not_applicable", False, "healthy"),
    ("HEALTHY", True, "healthy", True, "healthy"),
    ("WARNING", True, "warning", True, "warning"),
    ("CRITICAL", True, "critical", True, "critical"),
    ("UNKNOWN", True, "unknown", True, "unknown"),
    ("SOMETHING_NEW", True, "unknown", True, "unknown"),   # unrecognised status fails closed
])
def test_every_billing_status_maps_deterministically(tmp_path, monkeypatch, contract_status, enabled,
                                                     ceo_status, critical, overall):
    def fake(conn, config=None, *, now=None):
        return {"contract_version": 1, "status": contract_status, "enabled": enabled, "mode": "test",
                "configuration": {"ok": enabled, "problems": []}, "webhooks": {}, "incidents": {},
                "reasons": ["synthetic"]}
    monkeypatch.setattr(billing, "billing_health", fake)
    h = _eval(_healthy_db(tmp_path), ccfg())
    check = _billing_check(h)
    assert check["status"] == ceo_status and check["critical"] is critical
    assert h["overall_status"] == overall
    assert h["components"]["billing"]["status"] == ceo_status
    assert h["owner_alert_required"] is (overall != "healthy")


def test_contract_version_change_is_unknown_not_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(billing, "billing_health", lambda conn, config=None, now=None: {
        "contract_version": 2, "status": "HEALTHY", "enabled": True})
    h = _eval(_healthy_db(tmp_path), ccfg())
    assert _billing_check(h)["status"] == "unknown" and h["overall_status"] == "unknown"


def test_billing_contract_raising_is_unknown_not_healthy(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(billing, "billing_health", broken)
    h = _eval(_healthy_db(tmp_path), ccfg())
    check = _billing_check(h)
    assert check["status"] == "unknown" and check["critical"] is True
    assert check["evidence"] == {"state": "UNKNOWN", "error": "RuntimeError"}
    assert h["overall_status"] == "unknown" and h["owner_alert_required"] is True


# ------------------------------------------------- real contract, real DB -----

def test_not_enabled_is_neutral(tmp_path):
    h = _eval(_healthy_db(tmp_path), ccfg(CORRIDORIQ_BILLING_ENABLED="0"))
    check = _billing_check(h)
    assert check["status"] == "not_applicable" and check["critical"] is False
    assert check["evidence"]["state"] == "NOT_ENABLED" and check["evidence"]["enabled"] is False
    assert h["overall_status"] == "healthy" and h["owner_alert_required"] is False and h["incident_count"] == 0
    _assert_clean(h)


def test_enabled_and_clean_is_healthy(tmp_path):
    h = _eval(_healthy_db(tmp_path), ccfg())
    check = _billing_check(h)
    assert check["status"] == "healthy" and check["evidence"]["state"] == "HEALTHY"
    assert check["evidence"]["paid_subscriptions_by_account_type"] == {"contractor": 1}
    assert check["evidence"]["webhooks"]["unresolved_errors"] == 0
    assert h["overall_status"] == "healthy"
    _assert_clean(h)


def test_retrying_webhook_failure_is_a_visible_warning(tmp_path):
    path = _healthy_db(tmp_path)
    _with(path, lambda c: _event(c, "evt_TESTerror", "error", NOW - timedelta(hours=1)))
    h = _eval(path, ccfg())
    check = _billing_check(h)
    assert check["status"] == "warning" and "awaiting Stripe retry" in check["summary"]
    assert h["overall_status"] == "warning" and h["owner_alert_required"] is True
    incident = next(i for i in h["incidents"] if i["id"] == "billing")
    assert incident["severity"] == "warning" and "manual owner action" in incident["recommended_action"]
    _assert_clean(h)


def test_wrong_plan_payment_is_a_visible_warning(tmp_path):
    path = _healthy_db(tmp_path)
    _with(path, lambda c: c.execute(
        "INSERT INTO security_audit_log (event_type, details_json, created_at) VALUES "
        "('billing_price_mismatch', ?, ?)", (json.dumps({"subscription": SUB}), _iso(NOW - timedelta(days=1)))))
    h = _eval(path, ccfg())
    check = _billing_check(h)
    assert check["status"] == "warning"
    assert check["evidence"]["incidents"]["wrong_plan_payment_last_30d"] == 1
    assert h["overall_status"] == "warning"
    _assert_clean(h)   # the subscription ID in the audit row never reaches CEO output


def test_misconfiguration_is_critical(tmp_path):
    h = _eval(_healthy_db(tmp_path), ccfg(STRIPE_WEBHOOK_SECRET=None))
    check = _billing_check(h)
    assert check["status"] == "critical" and check["evidence"]["configuration_ok"] is False
    assert check["evidence"]["configuration_problem_count"] >= 1
    assert h["overall_status"] == "critical"
    _assert_clean(h)   # problem text names a secret variable, so only the count is carried


def test_failure_past_stripe_retry_window_is_critical(tmp_path):
    path = _healthy_db(tmp_path)
    _with(path, lambda c: _event(c, "evt_TESTold", "error", NOW - timedelta(days=4)))
    h = _eval(path, ccfg())
    assert _billing_check(h)["status"] == "critical" and h["overall_status"] == "critical"


def test_unreadable_billing_tables_are_unknown_never_healthy(tmp_path):
    h = _eval(_healthy_db(tmp_path, billing_tables=False), ccfg())
    check = _billing_check(h)
    assert check["status"] == "unknown" and check["critical"] is True and check["evidence"]["state"] == "UNKNOWN"
    assert h["overall_status"] == "unknown" and h["label"] == "CHECK INCOMPLETE — REVIEW NEEDED"
    assert any(i["id"] == "health_incomplete" for i in h["incidents"])


def test_unreadable_billing_tables_while_disabled_stay_neutral(tmp_path):
    h = _eval(_healthy_db(tmp_path, billing_tables=False), ccfg(CORRIDORIQ_BILLING_ENABLED="0"))
    assert _billing_check(h)["status"] == "not_applicable" and h["overall_status"] == "healthy"


def test_database_unavailable_with_billing_enabled_is_not_healthy(tmp_path):
    h = _eval(tmp_path / "missing.db", ccfg())
    assert _billing_check(h)["status"] == "unknown" and h["overall_status"] == "critical"


# ------------------------------------------- publication and side effects -----

@pytest.mark.parametrize("setup, expected", [
    (lambda c: None, "healthy"),
    (lambda c: _event(c, "evt_TESTerror", "error", NOW - timedelta(hours=1)), "warning"),
    (lambda c: _event(c, "evt_TESTold", "error", NOW - timedelta(days=4)), "critical"),
])
def test_published_health_record_keeps_billing_and_stays_valid(tmp_path, setup, expected):
    path = _healthy_db(tmp_path)
    _with(path, setup)
    h = _eval(path, ccfg())
    out = tmp_path / "intelligence"
    write_health(h, out)
    record, notice = load_health_record(out)
    assert notice is None and record is not None, "billing evidence must pass the health-file validator"
    assert record["components"]["billing"]["status"] == expected
    _assert_clean(record)


def test_billing_health_check_is_read_only(tmp_path):
    path = _healthy_db(tmp_path)
    _with(path, lambda c: _event(c, "evt_TESTerror", "error", NOW - timedelta(hours=1)))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    _eval(path, ccfg())
    _eval(path, ccfg(STRIPE_WEBHOOK_SECRET=None))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_ceo_billing_check_uses_the_existing_contract_only():
    import inspect
    src = inspect.getsource(ceo_health._billing)
    assert "billing_health" in src
    for forbidden in ("stripe.", "StripeGateway", "refund", "cancel(", "UPDATE ", "INSERT ", "DELETE "):
        assert forbidden not in src, forbidden
