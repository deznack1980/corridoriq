"""Approved analytics tools. Every number is computed here, not by a model.

Observed activity means a permit whose issued_date falls in the window and
whose project.contractor_company_id is that company. Stored why_now and
relevant_30d are model fields on company_customer_priority. They are returned
as stored text, not as the activity count.
"""

from __future__ import annotations

import sqlite3

from agents.ceo.analytics.guard import AnalyticsError, run_select
from agents.ceo.analytics.limits import (
    HIGH_PRIORITY_SCORE,
    MAX_ROWS,
    PLUMBING_IDENTITIES,
    PLUMBING_PROFILE,
    STALE_SOURCE_DAYS,
    USABLE_CONTACT_TYPES,
)
from agents.ceo.analytics.scope import AnalyticsScope
from agents.ceo.analytics.windows import bounds, parse_day, prior_bounds

_IDENTITY_SQL = ",".join("?" for _ in PLUMBING_IDENTITIES)
_CONTACT_SQL = ",".join("?" for _ in USABLE_CONTACT_TYPES)

_ACTIVITY = f"""
SELECT c.id AS company_id, c.display_name AS company_name, COUNT(pe.id) AS observed_permits
FROM companies c
JOIN projects pr ON pr.contractor_company_id = c.id
JOIN permits pe ON pe.id = pr.permit_id
JOIN company_customer_priority ccp ON ccp.company_id = c.id
WHERE ccp.profile_key = ?
  AND ccp.trade_identity IN ({_IDENTITY_SQL})
  AND substr(pe.issued_date, 1, 10) BETWEEN ? AND ?
GROUP BY c.id, c.display_name
ORDER BY observed_permits DESC, c.id
LIMIT ?
"""


def _envelope(tool: str, *, definition: str, claim_class: str, as_of: str, rows: list, extra: dict | None = None) -> dict:
    payload = {
        "tool": tool,
        "claim_class": claim_class,
        "definition": definition,
        "as_of": as_of,
        "source": "sqlite-readonly",
        "rows": rows,
        "row_count": len(rows),
    }
    if extra:
        payload.update(extra)
    return payload


def _clamp(limit: int | None) -> int:
    if limit is None:
        return MAX_ROWS
    try:
        value = int(limit)
    except (TypeError, ValueError) as exc:
        raise AnalyticsError("row limit is not an integer") from exc
    if value < 1:
        raise AnalyticsError("row limit must be positive")
    return min(value, MAX_ROWS)


def get_company_activity(conn, *, as_of: str, window_days: int, limit: int | None = None, city: str | None = None) -> dict:
    start, end = bounds(as_of, window_days)
    limit = _clamp(limit)
    sql = _ACTIVITY
    params: list = [PLUMBING_PROFILE, *PLUMBING_IDENTITIES, start, end]
    if city:
        sql = sql.replace("GROUP BY", "AND lower(pe.city) = lower(?) GROUP BY")
        params.append(city)
    params.append(limit)
    rows = run_select(conn, sql, tuple(params))
    return _envelope(
        "get_company_activity",
        definition="Count of plumbing-supply specialist or recurring-plumbing permits issued in the window.",
        claim_class="FACT",
        as_of=as_of,
        rows=rows,
        extra={"window": {"start": start, "end": end, "days": window_days}, "city": city},
    )


def compare_activity_periods(conn, *, as_of: str, window_days: int, limit: int | None = None) -> dict:
    current = get_company_activity(conn, as_of=as_of, window_days=window_days, limit=limit)
    prior_start, prior_end = prior_bounds(as_of, window_days)
    limit = _clamp(limit)
    prior_rows = run_select(
        conn,
        _ACTIVITY,
        (PLUMBING_PROFILE, *PLUMBING_IDENTITIES, prior_start, prior_end, limit),
    )
    prior = {row["company_id"]: row for row in prior_rows}
    current_ids = {row["company_id"] for row in current["rows"]}
    merged = []
    for row in current["rows"]:
        before = int((prior.get(row["company_id"]) or {}).get("observed_permits") or 0)
        after = int(row["observed_permits"])
        merged.append({
            "company_id": row["company_id"],
            "company_name": row["company_name"],
            "current_permits": after,
            "prior_permits": before,
            "delta": after - before,
        })
    for row in prior_rows:
        if row["company_id"] in current_ids:
            continue
        before = int(row["observed_permits"])
        merged.append({
            "company_id": row["company_id"],
            "company_name": row["company_name"],
            "current_permits": 0,
            "prior_permits": before,
            "delta": -before,
        })
    merged.sort(key=lambda item: (-item["delta"], -item["current_permits"], item["company_id"]))
    current_total = sum(item["current_permits"] for item in merged)
    prior_total = sum(item["prior_permits"] for item in merged)
    return _envelope(
        "compare_activity_periods",
        definition="Same observed-permit definition, current window minus the immediately previous window of equal length.",
        claim_class="CALCULATION",
        as_of=as_of,
        rows=merged[:limit],
        extra={
            "window_days": window_days,
            "current_window": current["window"],
            "prior_window": {"start": prior_start, "end": prior_end},
            "current_total": current_total,
            "prior_total": prior_total,
            "delta": current_total - prior_total,
        },
    )


def get_top_opportunities(conn, *, as_of: str, limit: int | None = None) -> dict:
    limit = _clamp(limit)
    rows = run_select(
        conn,
        f"""
        SELECT ccp.company_id, c.display_name AS company_name,
               ccp.account_priority_score, ccp.trade_identity,
               ccp.relevant_30d, ccp.most_recent_relevant_date,
               ccp.why_now, ccp.primary_demand_category, ccp.model_version, ccp.generated_at
        FROM company_customer_priority ccp
        JOIN companies c ON c.id = ccp.company_id
        WHERE ccp.profile_key = ?
          AND ccp.trade_identity IN ({_IDENTITY_SQL})
        ORDER BY ccp.account_priority_score DESC, ccp.company_id
        LIMIT ?
        """,
        (PLUMBING_PROFILE, *PLUMBING_IDENTITIES, limit),
    )
    return _envelope(
        "get_top_opportunities",
        definition="Current plumbing_supply priority rows for plumbing_specialist and recurring_plumbing, ordered by the stored account_priority_score. The score formula is not restated.",
        claim_class="FACT",
        as_of=as_of,
        rows=rows,
    )


def get_contact_gaps(conn, *, as_of: str, min_score: float = HIGH_PRIORITY_SCORE, limit: int | None = None) -> dict:
    limit = _clamp(limit)
    rows = run_select(
        conn,
        f"""
        SELECT ccp.company_id, c.display_name AS company_name, ccp.account_priority_score
        FROM company_customer_priority ccp
        JOIN companies c ON c.id = ccp.company_id
        WHERE ccp.profile_key = ?
          AND ccp.trade_identity IN ({_IDENTITY_SQL})
          AND ccp.account_priority_score >= ?
          AND NOT EXISTS (
            SELECT 1 FROM company_contact_channels ch
            WHERE ch.company_id = ccp.company_id
              AND ch.verification_status = 'VERIFIED'
              AND ch.status = 'active'
              AND ch.contact_type IN ({_CONTACT_SQL})
              AND ch.source_family != 'canonical_peer'
          )
        ORDER BY ccp.account_priority_score DESC, ccp.company_id
        LIMIT ?
        """,
        (PLUMBING_PROFILE, *PLUMBING_IDENTITIES, float(min_score), *USABLE_CONTACT_TYPES, limit),
    )
    covered = run_select(
        conn,
        f"""
        SELECT COUNT(*) AS high_priority,
               SUM(CASE WHEN EXISTS (
                 SELECT 1 FROM company_contact_channels ch
                 WHERE ch.company_id = ccp.company_id
                   AND ch.verification_status = 'VERIFIED'
                   AND ch.status = 'active'
                   AND ch.contact_type IN ({_CONTACT_SQL})
                   AND ch.source_family != 'canonical_peer'
               ) THEN 1 ELSE 0 END) AS with_usable_contact
        FROM company_customer_priority ccp
        WHERE ccp.profile_key = ?
          AND ccp.trade_identity IN ({_IDENTITY_SQL})
          AND ccp.account_priority_score >= ?
        """,
        (*USABLE_CONTACT_TYPES, PLUMBING_PROFILE, *PLUMBING_IDENTITIES, float(min_score)),
    )
    stats = covered[0] if covered else {"high_priority": 0, "with_usable_contact": 0}
    high = int(stats["high_priority"] or 0)
    usable = int(stats["with_usable_contact"] or 0)
    pct = None if high == 0 else round(100.0 * usable / high, 1)
    return _envelope(
        "get_contact_gaps",
        definition="High-priority plumbing identities with no active VERIFIED business phone or email on that company row. Canonical-peer channels do not count.",
        claim_class="CALCULATION",
        as_of=as_of,
        rows=rows,
        extra={"min_score": min_score, "high_priority": high, "with_usable_contact": usable, "usable_contact_pct": pct},
    )


def get_market_activity(conn, *, as_of: str, window_days: int, limit: int | None = None) -> dict:
    start, end = bounds(as_of, window_days)
    limit = _clamp(limit)
    rows = run_select(
        conn,
        f"""
        SELECT pe.jurisdiction, pe.city, COUNT(pe.id) AS observed_permits
        FROM permits pe
        JOIN projects pr ON pr.permit_id = pe.id
        JOIN company_customer_priority ccp ON ccp.company_id = pr.contractor_company_id
        WHERE ccp.profile_key = ?
          AND ccp.trade_identity IN ({_IDENTITY_SQL})
          AND substr(pe.issued_date, 1, 10) BETWEEN ? AND ?
        GROUP BY pe.jurisdiction, pe.city
        ORDER BY observed_permits DESC, pe.jurisdiction
        LIMIT ?
        """,
        (PLUMBING_PROFILE, *PLUMBING_IDENTITIES, start, end, limit),
    )
    return _envelope(
        "get_market_activity",
        definition="Observed plumbing-identity permits in the window, grouped by jurisdiction and city.",
        claim_class="FACT",
        as_of=as_of,
        rows=rows,
        extra={"window": {"start": start, "end": end, "days": window_days}},
    )


def get_feed_freshness(conn, *, as_of: str) -> dict:
    rows = run_select(
        conn,
        """
        SELECT s.jurisdiction_slug, s.health_state, s.newest_source_date,
               s.days_since_newest_source, s.records_7d, s.captured_at, s.last_success_at
        FROM source_health_snapshot s
        JOIN (
            SELECT jurisdiction_slug, MAX(captured_at) AS captured_at
            FROM source_health_snapshot
            GROUP BY jurisdiction_slug
        ) latest
          ON latest.jurisdiction_slug = s.jurisdiction_slug
         AND latest.captured_at = s.captured_at
        ORDER BY s.jurisdiction_slug
        """,
    )
    labeled = []
    for row in rows:
        days = row.get("days_since_newest_source")
        stale = days is not None and float(days) > STALE_SOURCE_DAYS
        thin = int(row.get("records_7d") or 0) == 0 and stale
        labeled.append({**row, "stale": stale, "thin": thin})
    return _envelope(
        "get_feed_freshness",
        definition=f"Latest source_health_snapshot per jurisdiction. Stale means days_since_newest_source > {STALE_SOURCE_DAYS}. Thin means stale and records_7d is 0.",
        claim_class="CALCULATION",
        as_of=as_of,
        rows=labeled,
    )


def get_pipeline_health(conn, *, as_of: str) -> dict:
    rows = run_select(
        conn,
        """
        SELECT id, run_type, status, started_at, completed_at,
               jurisdictions_attempted, jurisdictions_succeeded, jurisdictions_failed,
               records_received
        FROM pipeline_runs
        ORDER BY started_at DESC
        LIMIT 5
        """,
    )
    return _envelope(
        "get_pipeline_health",
        definition="Latest pipeline_runs rows. Status and record counts are stored facts.",
        claim_class="FACT",
        as_of=as_of,
        rows=rows,
    )


def get_recent_changes(conn, *, as_of: str) -> dict:
    runs = run_select(
        conn,
        """
        SELECT completed_at, started_at, status, records_received
        FROM pipeline_runs
        WHERE status = 'succeeded' AND completed_at IS NOT NULL
        ORDER BY completed_at DESC
        LIMIT 1
        """,
    )
    if not runs:
        return _envelope(
            "get_recent_changes",
            definition="Permits whose first_seen_at is at or after the latest successful pipeline_runs.completed_at.",
            claim_class="UNKNOWN",
            as_of=as_of,
            rows=[],
            extra={"reason": "no successful refresh"},
        )
    cutoff = runs[0]["completed_at"]
    rows = run_select(
        conn,
        """
        SELECT pe.id AS permit_id, pr.id AS project_id, pe.jurisdiction, pe.city,
               pe.issued_date, pe.first_seen_at, pr.contractor_company_id AS company_id
        FROM permits pe
        JOIN projects pr ON pr.permit_id = pe.id
        WHERE pe.first_seen_at >= ?
        ORDER BY pe.first_seen_at, pe.id
        LIMIT ?
        """,
        (cutoff, MAX_ROWS),
    )
    return _envelope(
        "get_recent_changes",
        definition="Permits first seen at or after the latest successful refresh completion.",
        claim_class="FACT",
        as_of=as_of,
        rows=rows,
        extra={"last_successful_refresh": cutoff, "new_permit_count": len(rows)},
    )


def get_ranking_changes(conn, *, as_of: str) -> dict:
    versions = run_select(
        conn,
        """
        SELECT model_version, MIN(created_at) AS created_at
        FROM sales_lane_snapshots
        WHERE lane_key = 'PLUMBING_CORE'
        GROUP BY model_version
        ORDER BY created_at
        """,
    )
    if len(versions) < 2:
        return _envelope(
            "get_ranking_changes",
            definition="Presentation rank movement requires two PLUMBING_CORE sales_lane_snapshots model versions.",
            claim_class="UNKNOWN",
            as_of=as_of,
            rows=[],
            extra={"reason": "insufficient history", "versions": len(versions)},
        )
    older, newer = versions[0]["model_version"], versions[-1]["model_version"]
    rows = run_select(
        conn,
        """
        SELECT n.primary_company_id AS company_id, n.canonical_name AS company_name,
               o.presentation_rank AS prior_rank, n.presentation_rank AS current_rank,
               (o.presentation_rank - n.presentation_rank) AS rank_delta
        FROM sales_lane_snapshots n
        JOIN sales_lane_snapshots o
          ON o.primary_company_id = n.primary_company_id
         AND o.lane_key = n.lane_key
         AND o.model_version = ?
        WHERE n.lane_key = 'PLUMBING_CORE' AND n.model_version = ?
        ORDER BY rank_delta DESC, n.primary_company_id
        LIMIT ?
        """,
        (older, newer, MAX_ROWS),
    )
    return _envelope(
        "get_ranking_changes",
        definition="Change in stored PLUMBING_CORE presentation_rank between the oldest and newest snapshot versions. This is not a rescoring.",
        claim_class="CALCULATION",
        as_of=as_of,
        rows=rows,
        extra={"older_version": older, "newer_version": newer},
    )


def get_opportunity_evidence(conn, *, as_of: str, company_id: int, window_days: int = 30) -> dict:
    start, end = bounds(as_of, window_days)
    rows = run_select(
        conn,
        """
        SELECT pe.id AS permit_id, pr.id AS project_id, pe.permit_number, pe.jurisdiction,
               pe.city, substr(pe.issued_date, 1, 10) AS issued_date,
               substr(COALESCE(pe.description, pe.project_description, ''), 1, 180) AS description
        FROM projects pr
        JOIN permits pe ON pe.id = pr.permit_id
        WHERE pr.contractor_company_id = ?
          AND substr(pe.issued_date, 1, 10) BETWEEN ? AND ?
        ORDER BY pe.issued_date DESC, pe.id
        LIMIT ?
        """,
        (int(company_id), start, end, MAX_ROWS),
    )
    return _envelope(
        "get_opportunity_evidence",
        definition="Permit and project identifiers for observed activity on one company in the window. Descriptions are truncated source text.",
        claim_class="FACT",
        as_of=as_of,
        rows=rows,
        extra={"company_id": int(company_id), "window": {"start": start, "end": end, "days": window_days}},
    )


def get_customer_book_activity(conn, scope: AnalyticsScope, *, as_of: str, window_days: int = 30) -> dict:
    org = scope.require_organization()
    start, end = bounds(as_of, window_days)
    rows = run_select(
        conn,
        """
        SELECT r.organization_id, r.company_id, c.display_name AS company_name,
               r.relationship_status,
               (
                 SELECT COUNT(*)
                 FROM projects pr
                 JOIN permits pe ON pe.id = pr.permit_id
                 WHERE pr.contractor_company_id = r.company_id
                   AND substr(pe.issued_date, 1, 10) BETWEEN ? AND ?
               ) AS observed_permits
        FROM crm_company_relationships r
        JOIN companies c ON c.id = r.company_id
        WHERE r.organization_id = ?
        ORDER BY observed_permits DESC, r.company_id
        LIMIT ?
        """,
        (start, end, int(org), MAX_ROWS),
    )
    return _envelope(
        "get_customer_book_activity",
        definition="CRM relationships for one organization only, with observed permit counts in the window. Other organizations are not readable.",
        claim_class="FACT",
        as_of=as_of,
        rows=rows,
        extra={"organization_id": int(org), "window": {"start": start, "end": end, "days": window_days}},
    )


def get_material_requests(_conn, scope: AnalyticsScope, *, as_of: str) -> dict:
    """Material requests are not in the intelligence warehouse. Do not open the pilot store."""
    if scope.kind == "organization":
        scope.require_organization()
    return _envelope(
        "get_material_requests",
        definition="Contractor material requests live in the pilot tenant store. This tool does not read that store.",
        claim_class="UNKNOWN",
        as_of=parse_day(as_of),
        rows=[],
        extra={"reason": "not in the intelligence database"},
    )


def tool_catalog() -> dict:
    return {
        "get_company_activity": get_company_activity,
        "compare_activity_periods": compare_activity_periods,
        "get_top_opportunities": get_top_opportunities,
        "get_contact_gaps": get_contact_gaps,
        "get_market_activity": get_market_activity,
        "get_feed_freshness": get_feed_freshness,
        "get_pipeline_health": get_pipeline_health,
        "get_recent_changes": get_recent_changes,
        "get_ranking_changes": get_ranking_changes,
        "get_opportunity_evidence": get_opportunity_evidence,
        "get_customer_book_activity": get_customer_book_activity,
        "get_material_requests": get_material_requests,
    }


def call_tool(conn: sqlite3.Connection, name: str, scope: AnalyticsScope, *, as_of: str, **kwargs) -> dict:
    catalog = tool_catalog()
    if name not in catalog:
        raise AnalyticsError("unknown analytics tool")
    fn = catalog[name]
    if name in {"get_customer_book_activity", "get_material_requests"}:
        return fn(conn, scope, as_of=as_of, **kwargs)
    return fn(conn, as_of=as_of, **kwargs)
