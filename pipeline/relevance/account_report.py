"""Shadow account-priority backtest report. Internal only."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import (
    ACCOUNT_PRIORITY_MODEL_VERSION,
    CUSTOMER_RELEVANCE_PROFILE,
    REPORTS_GENERATED_DIR,
)
from pipeline.relevance.account import score_distribution

KNOWN = [
    "Kerns Plumbing",
    "Parker & Sons",
    "Arizona Propane",
    "Millennium Gas",
    "Canyon State Propane",
    "JMax Mechanical",
    "Brincor",
    "Gas Piping Inc",
    "Saguaro Gas",
    "Advanced Plumbing",
    "ToyotaLift",
    "Creative Environments",
    "Pegasus Pool",
    "Lennar",
    "Shea Homes",
    "Pulte",
    "United Integrated",
    "Austin Commercial",
    "Marketech",
    "Okland",
    "City of Phoenix Water",
    "F D Electrical",
]


def top_accounts(conn: sqlite3.Connection, *, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        """
        SELECT a.*, c.display_name
        FROM company_customer_priority a
        JOIN companies c ON c.id = a.company_id
        WHERE a.profile_key=?
        ORDER BY a.account_priority_score DESC, a.relevant_90d DESC, a.company_id
        LIMIT ?
        """,
        (CUSTOMER_RELEVANCE_PROFILE, limit),
    )
    out = []
    for i, row in enumerate(rows, start=1):
        d = dict(row)
        d["rank"] = i
        try:
            d["demand_mix"] = json.loads(d.get("demand_categories") or "{}")
        except json.JSONDecodeError:
            d["demand_mix"] = {}
        out.append(d)
    return out


def known_rows(conn: sqlite3.Connection) -> list[dict]:
    ranked = {
        r["company_id"]: r
        for r in top_accounts(conn, limit=50000)
    }
    found = []
    for needle in KNOWN:
        matches = conn.execute(
            """
            SELECT a.*, c.display_name
            FROM company_customer_priority a
            JOIN companies c ON c.id = a.company_id
            WHERE a.profile_key=? AND (
                c.display_name LIKE ? OR c.normalized_name LIKE ?
            )
            ORDER BY a.account_priority_score DESC
            LIMIT 4
            """,
            (CUSTOMER_RELEVANCE_PROFILE, f"%{needle}%", f"%{needle.upper()}%"),
        ).fetchall()
        items = []
        for row in matches:
            d = dict(row)
            d["rank"] = ranked.get(d["company_id"], {}).get("rank")
            try:
                d["demand_mix"] = json.loads(d.get("demand_categories") or "{}")
            except json.JSONDecodeError:
                d["demand_mix"] = {}
            items.append(d)
        found.append({"needle": needle, "matches": items})
    return found


def demand_histogram(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = Counter()
    for row in conn.execute(
        "SELECT categories FROM project_demand_labels WHERE profile_key=?",
        (CUSTOMER_RELEVANCE_PROFILE,),
    ):
        try:
            labels = json.loads(row["categories"] or "[]")
        except json.JSONDecodeError:
            continue
        for lab in labels:
            counts[lab] += 1
    return dict(counts)


def write_account_report(
    conn: sqlite3.Connection,
    *,
    stats: dict,
    top: list[dict],
    known: list[dict],
) -> Path:
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_GENERATED_DIR / f"account_priority_shadow_{stamp}.md"
    dist = score_distribution(r["account_priority_score"] for r in top)
    hist = demand_histogram(conn)
    lines = [
        "# Plumbing-supply account priority (shadow)",
        "",
        "Internal only. Dashboard ranking and opportunity_score unchanged.",
        "",
        f"- model: `{stats.get('model_version', ACCOUNT_PRIORITY_MODEL_VERSION)}`",
        f"- accounts scored: {stats.get('accounts')}",
        f"- labeled projects: {stats.get('labeled_projects')}",
        f"- Top 100 unique scores: {dist['unique']}; largest tie: {dist['largest_tie']}",
        f"- scores r1/r10/r25/r50/r100: {dist['rank1']}/{dist['rank10']}/{dist['rank25']}/{dist['rank50']}/{dist['rank100']}",
        f"- spread rank 1–100: {dist['spread_1_100']}",
        "",
        "## Demand taxonomy (all labeled projects, multi-label)",
        "",
    ]
    for k, n in sorted(hist.items(), key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {n}")
    lines += ["", "## Top 100 accounts", ""]
    for r in top:
        mix = ", ".join(f"{k}:{v}" for k, v in list((r.get("demand_mix") or {}).items())[:5])
        lines.append(
            f"{r['rank']}. {r['display_name']} | score={r['account_priority_score']} "
            f"| {r['trade_identity']} | 30/90/180={r['relevant_30d']}/{r['relevant_90d']}/{r['relevant_180d']} "
            f"| high={r['high_relevance_project_count']} | {r['primary_demand_category']} | {r['why_now']}"
        )
        lines.append(f"    mix: {mix}")
    lines += ["", "## Known companies", ""]
    for block in known:
        lines.append(f"### {block['needle']}")
        if not block["matches"]:
            lines.append("- no account row")
            continue
        for m in block["matches"]:
            lines.append(
                f"- {m['display_name']} rank={m.get('rank')} score={m['account_priority_score']} "
                f"id={m['trade_identity']} 90d={m['relevant_90d']} primary={m['primary_demand_category']}"
            )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
