"""Shadow backtest for plumbing_supply relevance. Internal only."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import (
    CUSTOMER_RELEVANCE_MODEL_VERSION,
    CUSTOMER_RELEVANCE_PROFILE,
    REPORTS_GENERATED_DIR,
)


def summarize(conn: sqlite3.Connection, *, profile_key: str | None = None) -> dict:
    profile_key = profile_key or CUSTOMER_RELEVANCE_PROFILE
    row = conn.execute(
        """
        SELECT COUNT(*) total,
               COALESCE(SUM(CASE WHEN relevance_score >= 70 THEN 1 ELSE 0 END), 0) high,
               COALESCE(SUM(CASE WHEN relevance_score >= 40 AND relevance_score < 70 THEN 1 ELSE 0 END), 0) medium,
               COALESCE(SUM(CASE WHEN relevance_score >= 20 AND relevance_score < 40 THEN 1 ELSE 0 END), 0) borderline,
               COALESCE(AVG(relevance_score), 0) avg_score
        FROM project_customer_relevance
        WHERE profile_key=?
        """,
        (profile_key,),
    ).fetchone()
    incidental_high = conn.execute(
        """
        SELECT COUNT(*) FROM project_customer_relevance
        WHERE profile_key=? AND capability_class='incidental_project_scope'
          AND relevance_score >= 70
        """,
        (profile_key,),
    ).fetchone()[0]
    return {
        "profile_key": profile_key,
        "model_version": CUSTOMER_RELEVANCE_MODEL_VERSION,
        "total": int(row["total"] or 0),
        "high": int(row["high"] or 0),
        "medium": int(row["medium"] or 0),
        "borderline": int(row["borderline"] or 0),
        "avg_score": round(float(row["avg_score"] or 0), 1),
        "incidental_high": int(incidental_high or 0),
    }


def named_company_stats(conn: sqlite3.Connection, names: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for name in names:
        row = conn.execute(
            """
            SELECT COUNT(*) n,
                   COALESCE(MAX(r.relevance_score), 0) max_rel,
                   COALESCE(MAX(pr.opportunity_score), 0) max_opp,
                   COALESCE(AVG(r.relevance_score), 0) avg_rel
            FROM project_customer_relevance r
            JOIN companies c ON c.id = r.company_id
            JOIN projects pr ON pr.id = r.project_id
            WHERE r.profile_key=?
              AND (c.display_name LIKE ? OR c.normalized_name LIKE ?)
            """,
            (CUSTOMER_RELEVANCE_PROFILE, f"%{name}%", f"%{name.upper()}%"),
        ).fetchone()
        out[name] = {
            "projects": int(row["n"] or 0),
            "max_relevance": round(float(row["max_rel"] or 0), 1),
            "avg_relevance": round(float(row["avg_rel"] or 0), 1),
            "max_opportunity": round(float(row["max_opp"] or 0), 1),
        }
    return out


def top_rows(conn: sqlite3.Connection, *, order: str, limit: int = 8) -> list[dict]:
    if order == "relevance":
        order_sql = "r.relevance_score DESC"
    elif order == "opportunity":
        order_sql = "pr.opportunity_score DESC"
    else:
        raise ValueError(order)
    rows = conn.execute(
        f"""
        SELECT c.display_name, pr.project_category, pr.opportunity_score,
               r.relevance_score, r.demand_score, r.contractor_fit_basis,
               r.capability_class, p.permit_type, p.description
        FROM project_customer_relevance r
        JOIN projects pr ON pr.id = r.project_id
        JOIN permits p ON p.id = pr.permit_id
        LEFT JOIN companies c ON c.id = r.company_id
        WHERE r.profile_key=?
        ORDER BY {order_sql}
        LIMIT ?
        """,
        (CUSTOMER_RELEVANCE_PROFILE, limit),
    )
    return [dict(row) for row in rows]


def write_report(conn: sqlite3.Connection, stats: dict, *, named: dict,
                 top_relevance: list[dict], top_opportunity: list[dict]) -> Path:
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_GENERATED_DIR / f"customer_relevance_shadow_{stamp}.md"
    lines = [
        "# Customer relevance shadow backtest",
        "",
        "Internal only. opportunity_score and dashboard ranking are unchanged.",
        "",
        f"- profile: `{stats['profile_key']}`",
        f"- model: `{stats['model_version']}`",
        f"- scored projects: {stats['total']}",
        f"- high / medium / borderline: {stats['high']} / {stats['medium']} / {stats['borderline']}",
        f"- average relevance: {stats['avg_score']}",
        f"- incidental rows in high band: {stats['incidental_high']}",
        "",
        "## Named companies",
        "",
    ]
    for name, rec in named.items():
        lines.append(
            f"- {name}: projects={rec['projects']} max_relevance={rec['max_relevance']} "
            f"avg_relevance={rec['avg_relevance']} max_opportunity={rec['max_opportunity']}"
        )
    lines += ["", "## Top by plumbing_supply relevance", ""]
    for rec in top_relevance:
        lines.append(
            f"- {rec.get('display_name')} | rel={rec.get('relevance_score')} "
            f"opp={rec.get('opportunity_score')} | {rec.get('contractor_fit_basis')} | "
            f"{(rec.get('description') or '')[:80]}"
        )
    lines += ["", "## Top by opportunity_score (unchanged ranking)", ""]
    for rec in top_opportunity:
        lines.append(
            f"- {rec.get('display_name')} | opp={rec.get('opportunity_score')} "
            f"rel={rec.get('relevance_score')} | {rec.get('project_category')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
