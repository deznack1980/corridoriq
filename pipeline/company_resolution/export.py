"""Static JSON export for the Company Intelligence UI (offline fallback).

Mirrors the API shapes so companies.js works whether or not the API is running.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from pipeline.company_resolution.queries import _roles_for
from pipeline.config.settings import DATA_EXPORTS_DIR

_STATIC_LIST_LIMIT = 2000


def _write_json(name: str, payload: dict) -> None:
    DATA_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_EXPORTS_DIR / name).write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


def export_companies(conn: sqlite3.Connection) -> None:
    # Order and label by account relevance from the shared trust layer, so the
    # static directory agrees with the live portal. Internal scores are not exported.
    from pipeline.trust import account_view as trust
    from pipeline.trust.public import public_relevance

    rel_join, rel_rank, rel_params = trust.relevance_sql("c.id")
    rows = conn.execute(
        f"""
        SELECT c.id, c.display_name, c.normalized_name, c.city, c.state,
               c.company_type_primary, c.license_number,
               ci.company_priority_score, ci.company_priority_tier,
               ci.total_projects, ci.active_projects, ci.projects_last_30_days,
               ci.average_opportunity_score, ci.highest_opportunity_score,
               ci.municipality_count, ci.commercial_project_count,
               ci.residential_project_count, ci.activity_trend,
               ci.latest_activity_date, ci.estimated_opportunity_total
        FROM companies c
        LEFT JOIN company_intelligence ci ON ci.company_id=c.id
        {rel_join}
        WHERE c.lifecycle_state='active'
        ORDER BY {rel_rank} ASC, {trust.within_account_order()},
                 COALESCE(ci.company_priority_score,0) DESC,
                 ci.latest_activity_date DESC, c.display_name
        LIMIT ?
        """,
        (*rel_params, _STATIC_LIST_LIMIT),
    ).fetchall()
    relevance = trust.relevance_for(conn, [r["id"] for r in rows])
    items = []
    for r in rows:
        d = dict(r)
        d["roles"] = _roles_for(conn, r["id"])
        d["account_relevance"] = public_relevance(relevance.get(r["id"]))
        items.append(d)

    total = conn.execute(
        "SELECT COUNT(*) AS n FROM companies WHERE lifecycle_state='active'"
    ).fetchone()["n"]
    pending = conn.execute(
        "SELECT COUNT(*) AS n FROM company_match_review_queue WHERE lifecycle_state='pending'"
    ).fetchone()["n"]

    _write_json(
        "companies.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "total_companies": total,
            "pending_matches": pending,
            "shown": len(items),
            "items": items,
        },
    )


def export_match_review(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        """
        SELECT q.*, c.display_name AS candidate_name
        FROM company_match_review_queue q
        LEFT JOIN companies c ON c.id=q.candidate_company_id
        WHERE q.lifecycle_state='pending'
        ORDER BY q.match_confidence DESC LIMIT 1000
        """
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        for k in ("match_reasons", "conflicting_fields"):
            try:
                d[k] = json.loads(d[k]) if d[k] else []
            except (TypeError, ValueError):
                d[k] = []
        items.append(d)
    _write_json(
        "company_match_review.json",
        {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "count": len(items),
            "items": items,
        },
    )


def export_all(conn: sqlite3.Connection) -> None:
    export_companies(conn)
    export_match_review(conn)


if __name__ == "__main__":
    from pipeline.db.database import init_db

    connection = init_db()
    export_all(connection)
    print(f"Wrote company exports to {DATA_EXPORTS_DIR}")
    connection.close()
