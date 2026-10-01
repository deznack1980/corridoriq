"""Source freshness. A successful run is not the same as current coverage.

source_health_snapshot.health_state is driven by run success and deliberately
ignores how old the newest source record is. A jurisdiction whose feed
returns only year-old permits can report `healthy`. This module keeps both
readings and lets the older one win.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from pipeline.trust.dates import days_between, parse_date, parse_ts

FRESH = "FRESH"
LAGGING = "LAGGING"
STALE = "STALE"
NO_DATA = "NO_DATA"

FRESH_MAX_DAYS = 7
LAGGING_MAX_DAYS = 21

REFRESH_OK_HOURS = 36
REFRESH_BLOCK_DAYS = 7

_BAD_HEALTH = {"failing", "silent"}


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict] | None:
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except sqlite3.Error:
        return None


def freshness_for(newest: str | None, as_of: datetime) -> tuple[str, int | None]:
    days = days_between(parse_date(newest), as_of)
    if days is None:
        return NO_DATA, None
    if days <= FRESH_MAX_DAYS:
        return FRESH, days
    if days <= LAGGING_MAX_DAYS:
        return LAGGING, days
    return STALE, days


def collect_health(conn: sqlite3.Connection, as_of: datetime) -> dict:
    """Per-jurisdiction freshness plus the latest refresh run."""
    notes: list[str] = []
    jur = _rows(conn, "SELECT slug, name, status FROM jurisdictions ORDER BY slug") or []
    newest = _rows(
        conn,
        """
        SELECT jurisdiction,
               MAX(CASE WHEN substr(issued_date, 1, 10) <= ? THEN substr(issued_date, 1, 10) END) AS newest_issued,
               SUM(CASE WHEN substr(issued_date, 1, 10) > ? THEN 1 ELSE 0 END) AS future_dated,
               MAX(last_updated_at) AS last_touch,
               COUNT(*) AS permits
        FROM permits
        GROUP BY jurisdiction
        """,
        (as_of.date().isoformat(), as_of.date().isoformat()),
    )
    if newest is None:
        notes.append("permits table unavailable")
        newest = []
    by_slug = {r["jurisdiction"]: r for r in newest}
    snap = _rows(
        conn,
        """
        SELECT jurisdiction_slug, health_state, consecutive_failures, last_success_at,
               last_data_at, newest_source_date, failures_7d, runs_7d, captured_at
        FROM source_health_snapshot
        WHERE captured_at = (SELECT MAX(captured_at) FROM source_health_snapshot)
        """,
    )
    if snap is None:
        notes.append("source_health_snapshot unavailable; run health is UNKNOWN")
        snap = []
    health = {r["jurisdiction_slug"]: r for r in snap}

    sources = []
    known_slugs = [r["slug"] for r in jur if r.get("status") == "connected"] or sorted(by_slug)
    for slug in known_slugs:
        row = by_slug.get(slug) or {}
        state, days = freshness_for(row.get("newest_issued"), as_of)
        h = health.get(slug) or {}
        run_state = h.get("health_state") or "unknown"
        warnings = []
        if run_state in {"healthy", "unknown"} and state == STALE:
            warnings.append(
                f"run health says {run_state} but newest record is {days} days old; "
                "a successful run does not mean current coverage"
            )
        if run_state in _BAD_HEALTH:
            warnings.append(f"run health {run_state}")
        elif run_state == "degraded":
            warnings.append("run health degraded (repeated failures this week)")
        if int(row.get("future_dated") or 0):
            warnings.append(f"{row['future_dated']} future-dated permits ignored")
        blocked = state in {STALE, NO_DATA} or run_state in _BAD_HEALTH
        sources.append(
            {
                "jurisdiction": slug,
                "freshness": state,
                "newest_record_date": row.get("newest_issued"),
                "days_since_newest_record": days,
                "run_health": run_state,
                "last_success_at": h.get("last_success_at"),
                "failures_7d": h.get("failures_7d"),
                "permits": row.get("permits"),
                "call_blocked": blocked,
                "warnings": warnings,
            }
        )

    runs = _rows(
        conn,
        """
        SELECT id, run_type, status, started_at, completed_at,
               jurisdictions_attempted, jurisdictions_succeeded, jurisdictions_failed,
               records_received
        FROM pipeline_runs
        ORDER BY id DESC
        LIMIT 20
        """,
    )
    if runs is None:
        notes.append("pipeline_runs unavailable; refresh state is UNKNOWN")
        runs = []
    latest_ok = next((r for r in runs if (r.get("status") or "").lower() in {"succeeded", "success", "completed"}), None)
    latest_any = runs[0] if runs else None
    refresh_state, refresh_hours = _refresh_state(latest_ok, as_of)
    return {
        "as_of": as_of.isoformat(),
        "refresh": {
            "state": refresh_state,
            "hours_since_last_success": refresh_hours,
            "latest_success": latest_ok,
            "latest_run": latest_any,
        },
        "sources": sources,
        "stale_sources": [s["jurisdiction"] for s in sources if s["freshness"] in {STALE, NO_DATA}],
        "lagging_sources": [s["jurisdiction"] for s in sources if s["freshness"] == LAGGING],
        "degraded_sources": [
            s["jurisdiction"] for s in sources if s["run_health"] in _BAD_HEALTH | {"degraded"}
        ],
        "notes": notes,
    }


def _refresh_state(run: dict | None, as_of: datetime) -> tuple[str, float | None]:
    if run is None:
        return "UNKNOWN", None
    ts = parse_ts(run.get("completed_at") or run.get("started_at"))
    if ts is None:
        return "UNKNOWN", None
    hours = round((as_of - ts).total_seconds() / 3600.0, 1)
    if hours <= REFRESH_OK_HOURS:
        return "CURRENT", hours
    if hours <= REFRESH_BLOCK_DAYS * 24:
        return "AGING", hours
    return "STALE", hours


def source_for(health: dict, jurisdiction: str | None) -> dict:
    for s in health.get("sources") or []:
        if s["jurisdiction"] == jurisdiction:
            return s
    return {
        "jurisdiction": jurisdiction,
        "freshness": NO_DATA,
        "call_blocked": True,
        "warnings": ["jurisdiction not in connected source list"],
        "run_health": "unknown",
        "newest_record_date": None,
        "days_since_newest_record": None,
    }
