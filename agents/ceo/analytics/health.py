"""Deterministic system health for the CEO morning cycle.

A model is not asked whether CorridorIQ is healthy. Every status comes from
an explicit rule. automatic_remediation_permitted is false on every incident.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from agents.ceo.analytics.guard import AnalyticsError, open_analytics, run_select
from agents.ceo.analytics.limits import (
    FEED_BASELINE_POINTS,
    FEED_DROP_MIN_MEDIAN,
    FEED_DROP_RATIO,
    FEED_ZERO_MIN_PRIOR_RECORDS,
    REFRESH_CURRENT_HOURS,
    STALE_SOURCE_DAYS,
)
from agents.ceo.analytics.tools import get_contact_gaps, get_feed_freshness, get_pipeline_health, get_top_opportunities

SCHEMA = "corridoriq.ceo.health.v1"
HEALTH_NAME = "latest_health_status.json"
LABELS = {
    "healthy": "ALL SYSTEMS HEALTHY",
    "warning": "WARNING — REVIEW NEEDED",
    "critical": "CRITICAL — ACTION REQUIRED",
    "unknown": "CHECK INCOMPLETE — REVIEW NEEDED",
}
CRITICAL_TABLES = (
    "permits",
    "projects",
    "companies",
    "pipeline_runs",
    "jurisdictions",
    "source_health_snapshot",
    "users",
    "sessions",
)
_OVERALL = ("healthy", "warning", "critical", "unknown")
_SECRET_MARKERS = ("password", "token", "cookie", "secret", "api_key", "sk_live")


def evaluate_system_health(
    db_path: Path | None,
    *,
    refresh: dict | None = None,
    checked_at: str | None = None,
    billing_config=None,
) -> dict:
    """Read-only health result. Never raises. Never defaults to healthy on failure.

    ``billing_config`` is a ``pipeline.billing.config.BillingConfig``; by default
    the billing contract reads this process's billing environment."""
    checked = checked_at or _now()
    try:
        return _evaluate(db_path, refresh=refresh or {}, checked_at=checked, billing_config=billing_config)
    except Exception as exc:  # noqa: BLE001 - fail closed
        return incomplete_health(checked_at=checked, error=type(exc).__name__)


def incomplete_health(*, checked_at: str, error: str) -> dict:
    check = _check(
        "health_check",
        "system_health",
        "unknown",
        critical=True,
        claim_class="UNKNOWN",
        summary="The system health check did not finish.",
        rule="An unfinished check is CHECK INCOMPLETE, never healthy.",
        evidence={"error": error},
    )
    return _finalize([check], checked_at=checked_at, data_as_of=None, refresh_run_id=None)


def apply_brief_publication(health: dict, *, status: str, error: str | None = None) -> dict:
    """Fold the brief result into an already computed health report."""
    if status == "succeeded":
        check = _check(
            "brief_publication",
            "downstream_intelligence",
            "healthy",
            critical=True,
            claim_class="FACT",
            summary="The owner morning brief was published for this refresh.",
            rule="A succeeded refresh must produce a known owner brief.",
            evidence={},
        )
    elif status == "skipped":
        check = _check(
            "brief_publication",
            "downstream_intelligence",
            "not_applicable",
            critical=False,
            claim_class="FACT",
            summary="No new brief is published unless the refresh succeeded.",
            rule="A failed or partial refresh does not create a successful new brief.",
            evidence={},
        )
    else:
        check = _check(
            "brief_publication",
            "downstream_intelligence",
            "critical",
            critical=True,
            claim_class="FACT",
            summary="The refresh succeeded and the owner brief was not published.",
            rule="A succeeded refresh with a failed brief is not all-systems healthy.",
            evidence={"error": error or "brief generation failed"},
        )
    checks = [item for item in health.get("checks") or [] if item.get("id") != "brief_publication"]
    checks.append(check)
    return _finalize(
        checks,
        checked_at=health.get("checked_at") or _now(),
        data_as_of=health.get("data_as_of"),
        refresh_run_id=health.get("refresh_run_id"),
    )


def write_health(health: dict, directory: Path) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_public_record(health), indent=2, default=str)
    path = directory / HEALTH_NAME
    _atomic(path, text)
    written = {"json": str(path)}
    run_id = health.get("refresh_run_id")
    if isinstance(run_id, int):
        history = directory / "history"
        history.mkdir(parents=True, exist_ok=True)
        hist = history / f"health-refresh-{run_id:06d}.json"
        _atomic(hist, text)
        written["history"] = str(hist)
    return written


def load_health_record(directory: Path) -> tuple[dict | None, str | None]:
    """Return a valid health record and a notice when the latest file is unusable."""
    latest = directory / HEALTH_NAME
    parsed = _valid_health_file(latest)
    if parsed is not None:
        return parsed, None
    if latest.is_file():
        history = _newest_valid_history(directory / "history")
        if history is not None:
            return history, "The newest health file failed validation. Showing the last valid health record."
        return None, "The newest health file failed validation."
    history = _newest_valid_history(directory / "history")
    if history is not None:
        return history, "Showing the last valid health record."
    return None, None


def _evaluate(db_path: Path | None, *, refresh: dict, checked_at: str, billing_config=None) -> dict:
    if db_path is None or not Path(db_path).exists():
        unread = _check(
            "database",
            "database",
            "critical",
            critical=True,
            claim_class="FACT",
            summary="The intelligence database could not be opened read-only.",
            rule="Database health requires a readable SQLite file. No repair is attempted.",
            evidence={"reason": "database unavailable"},
        )
        return _finalize([unread, _billing(None, config=billing_config, checked_at=checked_at)],
                         checked_at=checked_at, data_as_of=None, refresh_run_id=refresh.get("run_id"))
    try:
        conn = open_analytics(Path(db_path))
    except AnalyticsError:
        unread = _check(
            "database",
            "database",
            "critical",
            critical=True,
            claim_class="FACT",
            summary="The intelligence database could not be opened read-only.",
            rule="Database health requires a readable SQLite file. No repair is attempted.",
            evidence={"reason": "database unavailable"},
        )
        return _finalize([unread, _billing(None, config=billing_config, checked_at=checked_at)],
                         checked_at=checked_at, data_as_of=None, refresh_run_id=refresh.get("run_id"))
    try:
        checks = [
            _database(conn),
            _schema(conn),
            _refresh(conn, checked_at),
            _feeds(conn, checked_at),
            _downstream(conn, checked_at),
            _application(conn),
            _billing(conn, config=billing_config, checked_at=checked_at),
        ]
    finally:
        conn.close()
    run_id = next((item.get("evidence", {}).get("run_id") for item in checks if item["id"] == "morning_refresh"), None)
    as_of = next((item.get("evidence", {}).get("completed_at") for item in checks if item["id"] == "morning_refresh"), None)
    if isinstance(as_of, str) and len(as_of) >= 10:
        as_of = as_of[:10]
    else:
        as_of = None
    if not isinstance(run_id, int):
        run_id = refresh.get("run_id") if isinstance(refresh.get("run_id"), int) else None
    return _finalize(checks, checked_at=checked_at, data_as_of=as_of, refresh_run_id=run_id)


def _database(conn) -> dict:
    try:
        rows = run_select(conn, "SELECT COUNT(*) AS n FROM permits")
        pipeline = run_select(
            conn,
            """
            SELECT COALESCE(SUM(records_received), 0) AS received
            FROM pipeline_runs
            WHERE run_type = 'morning_refresh' AND status = 'succeeded'
            """,
        )
    except AnalyticsError as exc:
        return _check(
            "database", "database", "critical", critical=True, claim_class="FACT",
            summary="A basic read of the intelligence database failed.",
            rule="Database health is a successful read-only count. No VACUUM or repair is run.",
            evidence={"error": str(exc)},
        )
    permits = int(rows[0]["n"]) if rows else 0
    received = int(pipeline[0]["received"]) if pipeline else 0
    if permits < 0 or received < 0:
        return _check(
            "database", "database", "critical", critical=True, claim_class="FACT",
            summary="A critical count was negative.",
            rule="Permit and refresh counts must be non-negative.",
            evidence={"permits": permits, "records_received": received},
        )
    if received > 0 and permits == 0:
        return _check(
            "database", "database", "critical", critical=True, claim_class="FACT",
            summary="Succeeded refreshes recorded received rows, and the permits table is empty.",
            rule="Operating history with records_received > 0 cannot pair with an empty permits table.",
            evidence={"permits": permits, "records_received": received},
        )
    return _check(
        "database", "database", "healthy", critical=True, claim_class="FACT",
        summary="The database opened read-only and basic counts were readable.",
        rule="Open read-only, count permits, and compare with succeeded refresh volume. No integrity rebuild.",
        evidence={"permits": permits, "records_received": received},
    )


def _schema(conn) -> dict:
    try:
        rows = run_select(conn, "SELECT name FROM sqlite_master WHERE type = 'table'")
    except AnalyticsError as exc:
        return _check(
            "schema", "database", "critical", critical=True, claim_class="FACT",
            summary="The table list could not be read.",
            rule="Critical schema is the presence of the operating tables.",
            evidence={"error": str(exc)},
        )
    present = {row["name"] for row in rows}
    missing = [name for name in CRITICAL_TABLES if name not in present]
    if missing:
        return _check(
            "schema", "database", "critical", critical=True, claim_class="FACT",
            summary="Critical tables are missing: " + ", ".join(missing) + ".",
            rule="permits, projects, companies, pipeline_runs, jurisdictions, source_health_snapshot, users, and sessions must exist.",
            evidence={"missing": missing},
        )
    return _check(
        "schema", "database", "healthy", critical=True, claim_class="FACT",
        summary="Critical operating and auth tables are present.",
        rule="Required table names are listed in CRITICAL_TABLES.",
        evidence={"tables": list(CRITICAL_TABLES)},
    )


def _refresh(conn, checked_at: str) -> dict:
    try:
        rows = run_select(
            conn,
            """
            SELECT id, status, started_at, completed_at, records_received
            FROM pipeline_runs
            WHERE run_type = 'morning_refresh'
            ORDER BY id DESC
            LIMIT 5
            """,
        )
    except AnalyticsError as exc:
        return _check(
            "morning_refresh", "morning_refresh", "critical", critical=True, claim_class="UNKNOWN",
            summary="Morning refresh history could not be read.",
            rule="The latest morning_refresh row is the refresh fact.",
            evidence={"error": str(exc)},
        )
    if not rows:
        return _check(
            "morning_refresh", "morning_refresh", "critical", critical=True, claim_class="FACT",
            summary="No morning refresh has been recorded.",
            rule="A missing morning refresh is critical. An older success is not implied.",
            evidence={},
        )
    latest = rows[0]
    prior_success = next((row for row in rows[1:] if row.get("status") == "succeeded"), None)
    evidence = {
        "run_id": latest.get("id"),
        "status": latest.get("status"),
        "completed_at": latest.get("completed_at"),
        "records_received": latest.get("records_received"),
        "last_known_success_at": None if prior_success is None else prior_success.get("completed_at"),
    }
    status = latest.get("status")
    if status == "failed":
        return _check(
            "morning_refresh", "morning_refresh", "critical", critical=True, claim_class="FACT",
            summary=f"The latest morning refresh failed (run {latest.get('id')}).",
            rule="The newest morning_refresh row decides refresh health. An older success does not.",
            evidence=evidence,
        )
    if status == "partial":
        return _check(
            "morning_refresh", "morning_refresh", "warning", critical=True, claim_class="FACT",
            summary=f"The latest morning refresh is partial (run {latest.get('id')}).",
            rule="Partial means at least one jurisdiction or stage did not finish cleanly.",
            evidence=evidence,
        )
    if status != "succeeded":
        return _check(
            "morning_refresh", "morning_refresh", "unknown", critical=True, claim_class="UNKNOWN",
            summary=f"The latest morning refresh status is {status}.",
            rule="Only succeeded, partial, and failed are classified. Anything else is incomplete.",
            evidence=evidence,
        )
    age = _hours_between(latest.get("completed_at"), checked_at)
    evidence["age_hours"] = age
    if age is None:
        return _check(
            "morning_refresh", "morning_refresh", "unknown", critical=True, claim_class="UNKNOWN",
            summary="The latest refresh succeeded, and its completion time could not be read.",
            rule=f"A current refresh completed within {REFRESH_CURRENT_HOURS} hours.",
            evidence=evidence,
        )
    if age > REFRESH_CURRENT_HOURS:
        return _check(
            "morning_refresh", "morning_refresh", "warning", critical=True, claim_class="CALCULATION",
            summary=f"The latest successful refresh is {age:.1f} hours old.",
            rule=f"Warning when completed_at is more than {REFRESH_CURRENT_HOURS} hours before the check.",
            evidence=evidence,
        )
    return _check(
        "morning_refresh", "morning_refresh", "healthy", critical=True, claim_class="FACT",
        summary=f"Morning refresh {latest.get('id')} succeeded and is current.",
        rule=f"Latest row is succeeded and completed within {REFRESH_CURRENT_HOURS} hours.",
        evidence=evidence,
    )


def _feeds(conn, checked_at: str) -> dict:
    try:
        expected = run_select(
            conn,
            "SELECT slug FROM jurisdictions WHERE status = 'connected' ORDER BY slug",
        )
        freshness = get_feed_freshness(conn, as_of=checked_at[:10])
    except AnalyticsError as exc:
        return _check(
            "feeds", "feeds", "unknown", critical=False, claim_class="UNKNOWN",
            summary="Feed freshness could not be read.",
            rule="Feed health uses get_feed_freshness and source_health_snapshot history.",
            evidence={"error": str(exc)},
        )
    by_slug = {row["jurisdiction_slug"]: row for row in freshness.get("rows") or []}
    problems = []
    unknowns = []
    for item in expected:
        slug = item["slug"]
        row = by_slug.get(slug)
        if row is None:
            problems.append({
                "source": slug,
                "severity": "warning",
                "claim_class": "FACT",
                "fact": f"{slug} is connected and has no freshness snapshot.",
                "impact": "That source is not known to be current.",
                "action": "Review the source after the next refresh. Do not treat it as current.",
                "rule": "Every connected jurisdiction needs a source_health_snapshot.",
                "last_known_healthy_at": None,
            })
            continue
        issue = _feed_issue(conn, row)
        if issue is None:
            continue
        if issue["severity"] == "unknown":
            unknowns.append(issue)
        else:
            problems.append(issue)
    evidence = {"connected": len(expected), "flagged": len(problems), "insufficient_baseline": len(unknowns)}
    if problems:
        summary = f"{len(problems)} connected source(s) need review."
        status = "warning"
    elif unknowns:
        summary = f"{len(unknowns)} connected source(s) lack a baseline. Zero or low volume was not called an anomaly."
        status = "unknown"
    elif not expected:
        return _check(
            "feeds", "feeds", "not_applicable", critical=False, claim_class="FACT",
            summary="No connected jurisdictions are configured.",
            rule="Feed checks apply to jurisdictions with status connected.",
            evidence=evidence,
        )
    else:
        return _check(
            "feeds", "feeds", "healthy", critical=False, claim_class="FACT",
            summary="Connected sources are inside the freshness and volume rules.",
            rule=f"Stale means days_since_newest_source > {STALE_SOURCE_DAYS}. Volume rules require {FEED_BASELINE_POINTS} prior snapshots.",
            evidence=evidence,
        )
    return _check(
        "feeds", "feeds", status, critical=False, claim_class="CALCULATION",
        summary=summary,
        rule=f"Stale > {STALE_SOURCE_DAYS} days. Thin means stale and records_7d = 0. Anomaly rules are in the health document.",
        evidence={**evidence, "issues": problems + unknowns},
    )


def _feed_issue(conn, row: dict) -> dict | None:
    slug = row.get("jurisdiction_slug")
    stale = bool(row.get("stale"))
    thin = bool(row.get("thin"))
    records = int(row.get("records_7d") or 0)
    state = row.get("health_state")
    if stale or thin or state in {"failing", "degraded", "silent"}:
        label = "thin and stale" if thin else "stale" if stale else str(state)
        return {
            "source": slug,
            "severity": "warning",
            "claim_class": "FACT" if stale or thin or state == "failing" else "CALCULATION",
            "fact": f"{slug} is {label}. records_7d={records}. days_since_newest_source={row.get('days_since_newest_source')}.",
            "impact": "Do not describe that city as current coverage.",
            "action": "Review the source. Do not repair it from this brief.",
            "rule": f"Stale when days_since_newest_source > {STALE_SOURCE_DAYS}. Thin when stale and records_7d is 0. Stored health_state failing, degraded, or silent is a warning.",
            "last_known_healthy_at": row.get("last_success_at"),
        }
    if records != 0 and state == "healthy":
        drop = _volume_drop(conn, slug, records)
        return drop
    if records == 0:
        return _zero_volume(conn, slug)
    return None


def _prior_volumes(conn, slug: str) -> list[int] | None:
    try:
        rows = run_select(
            conn,
            """
            SELECT records_7d FROM source_health_snapshot
            WHERE jurisdiction_slug = ?
            ORDER BY captured_at DESC
            LIMIT ?
            """,
            (slug, FEED_BASELINE_POINTS + 1),
        )
    except AnalyticsError:
        return None
    if len(rows) <= 1:
        return []
    return [int(row.get("records_7d") or 0) for row in rows[1:]]


def _zero_volume(conn, slug: str) -> dict:
    prior = _prior_volumes(conn, slug)
    enough = (
        prior is not None
        and len(prior) >= FEED_BASELINE_POINTS
        and all(value >= FEED_ZERO_MIN_PRIOR_RECORDS for value in prior[:FEED_BASELINE_POINTS])
    )
    fact = f"{slug} returned records_7d=0."
    if not enough:
        return {
            "source": slug,
            "severity": "unknown",
            "claim_class": "UNKNOWN",
            "fact": fact + " There is not a sufficient baseline to call that anomalous.",
            "impact": "Zero can be a quiet period. It is not classified as a failure.",
            "action": "Wait for more snapshot history before treating zero as an incident.",
            "rule": f"Anomalous zero requires {FEED_BASELINE_POINTS} prior snapshots each with records_7d >= {FEED_ZERO_MIN_PRIOR_RECORDS}.",
            "last_known_healthy_at": None,
        }
    return {
        "source": slug,
        "severity": "warning",
        "claim_class": "INFERENCE",
        "fact": fact,
        "impact": "This is low relative to the recent baseline. It is not proof the source is broken.",
        "action": "Compare the source with its recent snapshots before calling it down.",
        "rule": f"Warning when the latest records_7d is 0 and the prior {FEED_BASELINE_POINTS} snapshots were each >= {FEED_ZERO_MIN_PRIOR_RECORDS}.",
        "last_known_healthy_at": None,
    }


def _volume_drop(conn, slug: str, records: int) -> dict | None:
    prior = _prior_volumes(conn, slug)
    if prior is None or len(prior) < FEED_BASELINE_POINTS:
        return None
    sample = prior[:FEED_BASELINE_POINTS]
    median = statistics.median(sample)
    if median < FEED_DROP_MIN_MEDIAN:
        return None
    if records >= median * FEED_DROP_RATIO:
        return None
    return {
        "source": slug,
        "severity": "warning",
        "claim_class": "INFERENCE",
        "fact": f"{slug} records_7d={records}. Median of the prior {FEED_BASELINE_POINTS} snapshots is {median}.",
        "impact": "Volume is materially below the recent baseline. The cause is not identified.",
        "action": "Review the source. Do not invent a permit count or a repair.",
        "rule": f"Warning when latest < {FEED_DROP_RATIO} times a median of at least {FEED_DROP_MIN_MEDIAN}, using {FEED_BASELINE_POINTS} prior snapshots.",
        "last_known_healthy_at": None,
    }


def _downstream(conn, checked_at: str) -> dict:
    as_of = checked_at[:10]
    try:
        pipeline = get_pipeline_health(conn, as_of=as_of)
        freshness = get_feed_freshness(conn, as_of=as_of)
        opportunities = get_top_opportunities(conn, as_of=as_of)
        gaps = get_contact_gaps(conn, as_of=as_of)
    except AnalyticsError as exc:
        return _check(
            "downstream_analytics", "downstream_intelligence", "critical", critical=True, claim_class="FACT",
            summary="A governed analytics query failed after the database was readable.",
            rule="Pipeline, freshness, opportunities, and contact-gap queries must succeed.",
            evidence={"error": str(exc)},
        )
    for result in (pipeline, freshness, opportunities, gaps):
        if not isinstance(result, dict) or "rows" not in result:
            return _check(
                "downstream_analytics", "downstream_intelligence", "critical", critical=True, claim_class="FACT",
                summary="A governed analytics result was malformed.",
                rule="Each analytics tool returns a row list. A malformed result is critical.",
                evidence={"tool": None if not isinstance(result, dict) else result.get("tool")},
            )
    ranking = _ranking_probe(conn, as_of)
    return _check(
        "downstream_analytics", "downstream_intelligence", "healthy", critical=True, claim_class="FACT",
        summary="Pipeline, freshness, opportunity, and contact-gap queries completed.",
        rule="These reads are the downstream intelligence probe. Ranking with one snapshot stays UNKNOWN and is not a failure.",
        evidence={"ranking": ranking},
    )


def _ranking_probe(conn, as_of: str) -> str:
    try:
        from agents.ceo.analytics.tools import get_ranking_changes
        result = get_ranking_changes(conn, as_of=as_of)
    except AnalyticsError:
        return "unavailable"
    if result.get("claim_class") == "UNKNOWN":
        return "insufficient_history"
    return "available"


def _application(conn) -> dict:
    try:
        users = run_select(conn, "SELECT COUNT(*) AS n FROM users")
        sessions = run_select(conn, "SELECT COUNT(*) AS n FROM sessions")
    except AnalyticsError as exc:
        return _check(
            "application", "application", "critical", critical=True, claim_class="FACT",
            summary="The auth tables could not be read.",
            rule="Application health is a read-only count of users and sessions. No login is attempted.",
            evidence={"error": str(exc)},
        )
    return _check(
        "application", "application", "healthy", critical=True, claim_class="FACT",
        summary="Auth tables are readable.",
        rule="COUNT users and COUNT sessions succeed. This does not call a public URL.",
        evidence={"users": int(users[0]["n"]), "sessions": int(sessions[0]["n"])},
    )


_BILLING_STATUS = {
    "NOT_ENABLED": "not_applicable",
    "HEALTHY": "healthy",
    "WARNING": "warning",
    "CRITICAL": "critical",
    "UNKNOWN": "unknown",
}
_BILLING_CONTRACT = 1
_BILLING_RULE = (
    "Mapped from pipeline.billing.health.billing_health (contract v1, local application state only). "
    "NOT_ENABLED is neutral. While billing is enabled an UNKNOWN blocks ALL SYSTEMS HEALTHY. "
    "No Stripe API is called and nothing is refunded, cancelled or changed."
)
_BILLING_SUMMARY = {
    "not_applicable": "Billing is NOT_ENABLED. This is expected before billing activation.",
    "healthy": "Billing is enabled and its local webhook and configuration state shows no problem.",
    "warning": "Billing needs operator review.",
    "critical": "Billing is enabled and failing a critical billing rule.",
    "unknown": "Billing state could not be read, so billing is not reported healthy.",
}
_BILLING_WEBHOOK_FIELDS = ("last_event_received_at", "last_applied_at", "unresolved_errors",
                           "oldest_unresolved_error_at", "stuck_processing", "rejected_last_7d")
_BILLING_INCIDENT_FIELDS = ("wrong_plan_payment_last_30d", "duplicate_subscription_last_30d",
                            "webhook_rejected_last_30d")


def _billing(conn=None, *, config=None, checked_at: str | None = None) -> dict:
    """Consume the billing health contract. Read-only, no Stripe call, no billing mutation."""
    try:
        from pipeline.billing.health import billing_health

        report = billing_health(conn, config, now=_parse(checked_at) or datetime.now(timezone.utc))
    except Exception as exc:  # noqa: BLE001 - fail closed
        return _check(
            "billing", "billing", "unknown", critical=True, claim_class="UNKNOWN",
            summary=_BILLING_SUMMARY["unknown"], rule=_BILLING_RULE,
            evidence={"state": "UNKNOWN", "error": type(exc).__name__},
        )
    state = report.get("status")
    status = _BILLING_STATUS.get(state, "unknown")
    if report.get("contract_version") != _BILLING_CONTRACT:
        state, status = "UNKNOWN", "unknown"
    enabled = bool(report.get("enabled"))
    # CEO evidence is an explicit allow-list: counts, booleans and timestamps.
    # Never configuration names or values, Stripe IDs or payment details.
    evidence = {
        "state": state if status != "unknown" else "UNKNOWN",
        "enabled": enabled,
        "mode": report.get("mode") if report.get("mode") in ("test", "live") else None,
        "basis": "local_application_state",
        "configuration_ok": bool((report.get("configuration") or {}).get("ok")),
        "configuration_problem_count": len((report.get("configuration") or {}).get("problems") or []),
        "webhooks": {k: (report.get("webhooks") or {}).get(k) for k in _BILLING_WEBHOOK_FIELDS},
        "incidents": {k: (report.get("incidents") or {}).get(k) for k in _BILLING_INCIDENT_FIELDS},
        "paid_subscriptions_by_account_type": {
            str(k): int(v) for k, v in (report.get("subscriptions_in_paid_state") or {}).items()
        },
        "reasons": [r for r in report.get("reasons") or []
                    if isinstance(r, str) and not any(m in r.lower() for m in _SECRET_MARKERS)],
    }
    summary = _BILLING_SUMMARY[status]
    if status in ("warning", "critical") and evidence["reasons"]:
        summary = summary + " " + "; ".join(evidence["reasons"]) + "."
    return _check(
        "billing", "billing", status,
        critical=enabled or status == "unknown",
        claim_class="UNKNOWN" if status == "unknown" else "FACT",
        summary=summary, rule=_BILLING_RULE, evidence=evidence,
    )


def _check(check_id, component, status, *, critical, claim_class, summary, rule, evidence) -> dict:
    return {
        "id": check_id,
        "component": component,
        "status": status,
        "critical": critical,
        "claim_class": claim_class,
        "summary": summary,
        "rule": rule,
        "evidence": evidence,
    }


def _finalize(checks: list[dict], *, checked_at: str, data_as_of: str | None, refresh_run_id: int | None) -> dict:
    overall = _aggregate(checks)
    incidents = _incidents(checks, checked_at)
    alert = overall in {"warning", "critical", "unknown"}
    reason = {
        "healthy": "No warning or critical check failed.",
        "warning": "At least one check is a warning.",
        "critical": "At least one critical check failed.",
        "unknown": "A critical check is incomplete, so the system is not reported healthy.",
    }[overall]
    return {
        "schema": SCHEMA,
        "overall_status": overall,
        "label": LABELS[overall],
        "checked_at": checked_at,
        "data_as_of": data_as_of,
        "refresh_run_id": refresh_run_id,
        "scope": {"kind": "owner", "organization_id": None},
        "execute": False,
        "checks": checks,
        "incidents": incidents,
        "owner_alert_required": alert,
        "alert_reason": reason,
        "alert_severity": overall,
        "incident_count": len(incidents),
        "components": _components(checks),
    }


def _aggregate(checks: list[dict]) -> str:
    if any(item["status"] == "critical" for item in checks):
        return "critical"
    if any(item.get("critical") and item["status"] == "unknown" for item in checks):
        return "unknown"
    if any(item["status"] == "warning" for item in checks):
        return "warning"
    if any(item.get("critical") and item["status"] not in {"healthy", "not_applicable"} for item in checks):
        return "unknown"
    return "healthy"


def _incidents(checks: list[dict], checked_at: str) -> list[dict]:
    found = []
    for item in checks:
        issues = (item.get("evidence") or {}).get("issues")
        if item["id"] == "feeds" and isinstance(issues, list):
            for issue in issues:
                if issue.get("severity") not in {"warning", "critical"}:
                    continue
                found.append(_incident(
                    item["id"] + ":" + str(issue.get("source")),
                    issue.get("severity"),
                    "feeds",
                    issue.get("source"),
                    checked_at,
                    issue.get("last_known_healthy_at"),
                    issue.get("fact"),
                    issue.get("impact"),
                    issue.get("action"),
                    issue.get("rule"),
                    issue.get("claim_class") or "FACT",
                ))
            continue
        if item["status"] not in {"warning", "critical"}:
            continue
        found.append(_incident(
            item["id"],
            item["status"],
            item["component"],
            item["component"],
            checked_at,
            (item.get("evidence") or {}).get("last_known_success_at"),
            item["summary"],
            _impact(item),
            _action(item),
            item["rule"],
            item.get("claim_class") or "FACT",
        ))
    if any(item.get("critical") and item["status"] == "unknown" for item in checks):
        found.append(_incident(
            "health_incomplete",
            "unknown",
            "system_health",
            "system_health",
            checked_at,
            None,
            "A critical check could not be completed.",
            "The owner cannot treat the system as healthy.",
            "Review the incomplete check. Do not assume a pass.",
            "A critical UNKNOWN blocks ALL SYSTEMS HEALTHY.",
            "UNKNOWN",
        ))
    return found


def _incident(incident_id, severity, component, source, detected_at, last_healthy, fact, impact, action, rule, claim_class) -> dict:
    return {
        "id": incident_id,
        "severity": severity if severity in {"warning", "critical", "unknown"} else "warning",
        "component": component,
        "source": source,
        "detected_at": detected_at,
        "last_known_healthy_at": last_healthy,
        "fact": fact,
        "claim_class": claim_class,
        "impact": impact,
        "recommended_action": action,
        "automatic_remediation_permitted": False,
        "rule": rule,
    }


def _impact(item: dict) -> str:
    if item["id"] == "morning_refresh":
        return "Today's intelligence must not be described as current from an older success."
    if item["component"] == "database":
        return "Counts and briefs that depend on this database are not trustworthy."
    if item["id"] == "downstream_analytics":
        return "The refresh may have finished while the owner brief inputs did not."
    if item["id"] == "brief_publication":
        return "The owner would be looking at an older brief, or at no brief."
    if item["id"] == "application":
        return "Sign-in and the admin brief cannot be assumed to be available."
    if item["id"] == "billing":
        return "Paid access and Contractor Pro priority may not match Stripe until this is reviewed."
    return "The owner should review this check before treating the morning as normal."


def _action(item: dict) -> str:
    if item["id"] == "morning_refresh":
        return "Inspect the latest morning refresh. Do not rerun it from this health check."
    if item["component"] == "database":
        return "Restore access from the existing operational process. This check will not repair the file."
    if item["id"] == "brief_publication":
        return "Read the brief failure status. Keep the previous valid brief labeled as older."
    if item["id"] == "billing":
        return ("Review the admin billing health and billing incidents views. Refunds and cancellations "
                "are a manual owner action in Stripe. Automatic remediation is off.")
    return "Review the evidence on this check. Automatic remediation is off."


def _components(checks: list[dict]) -> dict:
    groups = {
        "refresh": ["morning_refresh"],
        "database": ["database", "schema"],
        "feeds": ["feeds"],
        "downstream": ["downstream_analytics", "brief_publication"],
        "application": ["application"],
        "billing": ["billing"],
    }
    by_id = {item["id"]: item for item in checks}
    out = {}
    for name, ids in groups.items():
        chosen = [by_id[item] for item in ids if item in by_id]
        status = _worst(item["status"] for item in chosen) if chosen else "unknown"
        summary = chosen[0]["summary"] if len(chosen) == 1 else "; ".join(item["summary"] for item in chosen)
        out[name] = {"status": status, "summary": summary}
    return out


def _worst(statuses) -> str:
    order = ["critical", "unknown", "warning", "healthy", "not_applicable"]
    found = list(statuses)
    for status in order:
        if status in found:
            return status
    return "unknown"


def _public_record(health: dict) -> dict:
    """Drop nothing operational, and never add a secret field."""
    return {
        "schema": health.get("schema"),
        "overall_status": health.get("overall_status"),
        "label": health.get("label"),
        "checked_at": health.get("checked_at"),
        "data_as_of": health.get("data_as_of"),
        "refresh_run_id": health.get("refresh_run_id"),
        "scope": {"kind": "owner", "organization_id": None},
        "execute": False,
        "checks": health.get("checks") or [],
        "incidents": health.get("incidents") or [],
        "owner_alert_required": bool(health.get("owner_alert_required")),
        "alert_reason": health.get("alert_reason"),
        "alert_severity": health.get("alert_severity"),
        "incident_count": health.get("incident_count"),
        "components": health.get("components") or {},
    }


def _valid_health_file(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != SCHEMA or data.get("overall_status") not in _OVERALL:
        return None
    if not isinstance(data.get("checks"), list) or not isinstance(data.get("incidents"), list):
        return None
    scope = data.get("scope") or {}
    if scope.get("kind") != "owner" or scope.get("organization_id") is not None:
        return None
    if data.get("execute") is not False:
        return None
    blob = json.dumps(data).lower()
    if any(marker in blob for marker in _SECRET_MARKERS):
        return None
    for incident in data["incidents"]:
        if not isinstance(incident, dict) or incident.get("automatic_remediation_permitted") is not False:
            return None
    data["label"] = LABELS.get(data["overall_status"], LABELS["unknown"])
    return data


def _newest_valid_history(history: Path) -> dict | None:
    if not history.is_dir():
        return None
    for path in sorted(history.glob("health-refresh-*.json"), reverse=True):
        parsed = _valid_health_file(path)
        if parsed is not None:
            return parsed
    return None


def _hours_between(start: str | None, end: str | None) -> float | None:
    left = _parse(start)
    right = _parse(end)
    if left is None or right is None:
        return None
    return (right - left).total_seconds() / 3600


def _parse(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _atomic(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
