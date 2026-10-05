"""Billing health and operator incidents: the integration contract for system-health layers.

`billing_health(conn)` reports what CorridorIQ can prove from its OWN state: the
configuration it was started with (names and booleans only) and what its
webhook/audit tables recorded. It never calls Stripe, never includes keys,
webhook secrets, Stripe customer IDs or payment details, and makes no claim
about Stripe's or the network's availability.

Status vocabulary (stable; consumers should switch on `status`):

    NOT_ENABLED  billing is switched off (normal outside a billing launch; not an error)
    HEALTHY      enabled and configured; no locally observed problems
    WARNING      operator attention: retrying webhook failures, rejected or stuck
                 events, wrong-plan or duplicate subscriptions that may need a refund
    CRITICAL     enabled but misconfigured, or webhook failures older than Stripe's
                 3-day retry window (events will not be redelivered)
    UNKNOWN      billing state could not be read (e.g. tables missing)

`billing_incidents(conn)` lists the operator-actionable incidents behind a
WARNING: organization, account type, expected plan, mismatch type, the Stripe
subscription involved, and when it happened. Refunds stay a manual decision.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from pipeline.billing.config import load_config
from pipeline.billing.plans import PLANS, plan_for_account_type

log = logging.getLogger(__name__)

NOT_ENABLED, HEALTHY, WARNING, CRITICAL, UNKNOWN = "NOT_ENABLED", "HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"
CONTRACT_VERSION = 1

STRIPE_RETRY_WINDOW = timedelta(days=3)     # Stripe stops redelivering after ~3 days
STUCK_PROCESSING_AFTER = timedelta(minutes=15)
INCIDENT_WINDOW = timedelta(days=30)
REJECTION_WINDOW = timedelta(days=7)

INCIDENT_EVENTS = {
    "billing_price_mismatch": "wrong_plan_payment",
    "billing_duplicate_subscription": "duplicate_subscription",
    "billing_webhook_rejected": "webhook_rejected",
}
OPERATOR_ACTION = {
    "wrong_plan_payment": "Review the subscription in Stripe; cancel and refund manually if payment was collected. "
                          "No CorridorIQ access was granted.",
    "duplicate_subscription": "A second live subscription exists; cancel and refund the extra one in Stripe. "
                              "The original subscription remains in force.",
    "webhook_rejected": "A Stripe event did not match CorridorIQ's records and was not applied; review if repeated.",
}


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _scalar(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


def billing_health(conn, config=None, *, now=None) -> dict:
    cfg = config or load_config()
    now = now or datetime.now(timezone.utc)
    problems = list(cfg.problems()) if cfg.enabled else []
    if cfg.enabled:
        problems += [p for p in cfg.webhook_problems() if p not in problems]
    report = {
        "contract_version": CONTRACT_VERSION,
        "component": "billing",
        "basis": "local_application_state",
        "generated_at": _iso(now),
        "enabled": cfg.enabled,
        "mode": cfg.mode,
        "configuration": {
            "ok": cfg.enabled and not problems,
            "problems": problems,  # variable names / rule descriptions only, never values
            "webhook_secret_configured": bool(cfg.webhook_secret),
            "plans": {p.key: {"price_configured": bool(cfg.price_id_for(p))} for p in PLANS.values()},
        },
        "webhooks": {},
        "incidents": {},
        "reasons": [],
    }
    if not cfg.enabled:
        report["status"] = NOT_ENABLED
        report["reasons"].append("billing is not enabled (CORRIDORIQ_BILLING_ENABLED is not 1)")
        return report

    try:
        since_retry = _iso(now - STRIPE_RETRY_WINDOW)
        wh = {
            "last_event_received_at": _scalar(conn, "SELECT MAX(received_at) FROM billing_stripe_events"),
            "last_applied_at": _scalar(conn, "SELECT MAX(processed_at) FROM billing_stripe_events "
                                             "WHERE outcome LIKE 'applied%'"),
            "unresolved_errors": _scalar(conn, "SELECT COUNT(*) FROM billing_stripe_events WHERE outcome='error'"),
            "oldest_unresolved_error_at": _scalar(conn, "SELECT MIN(received_at) FROM billing_stripe_events "
                                                        "WHERE outcome='error'"),
            "stuck_processing": _scalar(conn, "SELECT COUNT(*) FROM billing_stripe_events WHERE outcome='processing' "
                                              "AND received_at < ?", (_iso(now - STUCK_PROCESSING_AFTER),)),
            "rejected_last_7d": _scalar(conn, "SELECT COUNT(*) FROM billing_stripe_events WHERE outcome LIKE 'rejected%' "
                                              "AND received_at >= ?", (_iso(now - REJECTION_WINDOW),)),
        }
        since_inc = _iso(now - INCIDENT_WINDOW)
        inc = {kind: _scalar(conn, "SELECT COUNT(*) FROM security_audit_log WHERE event_type=? AND created_at >= ?",
                             (event_type, since_inc))
               for event_type, kind in INCIDENT_EVENTS.items()}
        paid = {}
        for row in conn.execute("SELECT o.account_type, COUNT(*) FROM billing_accounts b JOIN organizations o "
                                "ON o.id=b.organization_id WHERE b.billing_state IN ('active','trialing') "
                                "GROUP BY o.account_type"):
            paid[row[0]] = row[1]
    except Exception:
        log.warning("billing health could not read billing state")
        report["status"] = UNKNOWN
        report["reasons"].append("billing tables could not be read")
        return report

    report["webhooks"] = wh
    report["incidents"] = {f"{k}_last_30d": v for k, v in inc.items()}
    report["subscriptions_in_paid_state"] = paid   # counts only, by account type

    status, reasons = HEALTHY, []
    if problems:
        status = CRITICAL
        reasons.append("billing is enabled but misconfigured")
    if wh["unresolved_errors"]:
        old = wh["oldest_unresolved_error_at"] and wh["oldest_unresolved_error_at"] < since_retry
        if old:
            status = CRITICAL
            reasons.append("webhook failures older than Stripe's retry window (manual replay needed)")
        else:
            status = status if status == CRITICAL else WARNING
            reasons.append(f"{wh['unresolved_errors']} webhook event(s) failed and are awaiting Stripe retry")
    if wh["stuck_processing"]:
        status = status if status == CRITICAL else WARNING
        reasons.append(f"{wh['stuck_processing']} webhook event(s) stuck in processing")
    if wh["rejected_last_7d"]:
        status = status if status == CRITICAL else WARNING
        reasons.append(f"{wh['rejected_last_7d']} webhook event(s) rejected in the last 7 days")
    if inc["wrong_plan_payment"] or inc["duplicate_subscription"]:
        status = status if status == CRITICAL else WARNING
        reasons.append("wrong-plan or duplicate subscription(s) need operator review (possible refund)")
    if wh["last_event_received_at"] is None:
        reasons.append("no webhook events recorded yet (cannot confirm delivery from local state)")
    report["status"] = status
    report["reasons"] = reasons
    return report


def billing_incidents(conn, *, now=None, window=INCIDENT_WINDOW, limit=100) -> list[dict]:
    """Operator view of wrong-plan payments, duplicate subscriptions and rejected events."""
    now = now or datetime.now(timezone.utc)
    placeholders = ",".join("?" for _ in INCIDENT_EVENTS)
    rows = conn.execute(
        f"SELECT a.id, a.event_type, a.details_json, a.created_at, a.organization_id, o.name, o.account_type "
        f"FROM security_audit_log a LEFT JOIN organizations o ON o.id = a.organization_id "
        f"WHERE a.event_type IN ({placeholders}) AND a.created_at >= ? ORDER BY a.id DESC LIMIT ?",
        (*INCIDENT_EVENTS, _iso(now - window), limit)).fetchall()
    out = []
    for r in rows:
        try:
            details = json.loads(r["details_json"] or "{}")
        except ValueError:
            details = {}
        kind = INCIDENT_EVENTS[r["event_type"]]
        expected = plan_for_account_type(r["account_type"])
        out.append({
            "incident": kind,
            "occurred_at": r["created_at"],
            "organization_id": r["organization_id"],
            "organization_name": r["name"],
            "account_type": r["account_type"],
            "expected_plan": expected.key if expected else None,
            "mismatch_type": details.get("reason") or (kind if kind != "webhook_rejected" else None),
            "observed_plan": details.get("observed_plan"),
            "stripe_subscription": details.get("subscription"),
            "stripe_event_type": details.get("stripe_event"),
            "operator_action": OPERATOR_ACTION[kind],
        })
    return out
