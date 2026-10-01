"""Phase 4F internal sales-lane report. Not a customer surface."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import REPORTS_GENERATED_DIR, ROC_DATA_DIR, SALES_LANE_VERSION
from pipeline.entity.report import score_guardrails
from pipeline.relevance.account_report import top_accounts
from pipeline.sales_lanes.collapse import collapse_stats
from pipeline.sales_lanes.known import KNOWN
from pipeline.sales_lanes.lanes import IDENTITY_REVIEW, SALESPERSON_LANES
from pipeline.sales_lanes.quality import AMBIGUOUS, CLEARLY_BELONGS, CLEARLY_WRONG, PROBABLY_BELONGS, PROBABLY_WRONG


def _quality_pct(rows: list[dict]) -> dict:
    n = len(rows) or 1
    counts = {}
    for r in rows:
        counts[r.get("quality_label") or "UNLABELED"] = counts.get(r.get("quality_label") or "UNLABELED", 0) + 1
    return {k: {"n": v, "pct": round(100.0 * v / n, 1)} for k, v in counts.items()}


def known_membership(conn: sqlite3.Connection) -> list[dict]:
    out = []
    seen = set()
    for needle in KNOWN:
        rows = conn.execute(
            """
            SELECT c.id, c.display_name, a.account_priority_score, a.trade_identity
            FROM companies c
            LEFT JOIN company_customer_priority a
              ON a.company_id=c.id AND a.profile_key='plumbing_supply'
            WHERE c.display_name LIKE ?
            ORDER BY a.account_priority_score DESC
            LIMIT 3
            """,
            (f"%{needle}%",),
        )
        for row in rows:
            cid = int(row["id"])
            if cid in seen:
                continue
            seen.add(cid)
            lanes = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT lane_key, fit, subtype, presentable, evidence
                    FROM company_sales_lanes
                    WHERE company_id=? AND model_version=?
                    """,
                    (cid, SALES_LANE_VERSION),
                )
            ]
            out.append(
                {
                    "company_id": cid,
                    "display_name": row["display_name"],
                    "account_priority_score": row["account_priority_score"],
                    "trade_identity": row["trade_identity"],
                    "lanes": lanes,
                }
            )
    return out


def write_phase4f_report(
    conn: sqlite3.Connection,
    *,
    books: dict[str, list[dict]],
    assign_stats: dict,
    collapse: dict,
    html_path: Path,
    elapsed_s: float,
    before_guard: dict,
    after_guard: dict,
) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    ROC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    known = known_membership(conn)
    plumbing = books.get("PLUMBING_CORE") or []
    q = _quality_pct(plumbing)
    payload = {
        "version": SALES_LANE_VERSION,
        "assign": assign_stats,
        "collapse_top100": collapse,
        "quality": q,
        "books": {k: v for k, v in books.items()},
        "known": known,
        "guardrails_before": before_guard,
        "guardrails_after": after_guard,
        "elapsed_s": elapsed_s,
        "html": str(html_path),
    }
    snap = ROC_DATA_DIR / f"sales_lanes_{ts}.json"
    snap.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    path = REPORTS_GENERATED_DIR / f"sales_lanes_{ts}.md"
    lines = [
        f"# CorridorIQ Phase 4F — Internal sales lanes ({ts})",
        "",
        "Presentation layer only. `account_priority_score` was not recalculated.",
        "Customer dashboard was not replaced.",
        "",
        "## A. Architecture",
        "",
        "Lanes: " + ", ".join(SALESPERSON_LANES) + f", plus `{IDENTITY_REVIEW}`.",
        "Membership is multi-label. Fit is HIGH/MEDIUM/LOW/EXCLUDED and is not a replacement rank.",
        "",
        "## G. Canonical collapse (Top 100 raw priority rows)",
        "",
        json.dumps(collapse, indent=2),
        "",
        "## I. Plumbing-core quality",
        "",
        json.dumps(q, indent=2),
        f"CLEARLY/PROBABLY belongs should dominate. Flags: {CLEARLY_WRONG}, {PROBABLY_WRONG}, {AMBIGUOUS}, {PROBABLY_BELONGS}, {CLEARLY_BELONGS}.",
        "",
        "## H. Plumbing-core Top 25",
        "",
        "| Rk | Canonical | Pri | Trade | Identity | 30/90/180 | Demand | Phone | Quality |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in plumbing:
        lines.append(
            f"| {r['presentation_rank']} | {r['canonical_name']} | {r['account_priority_score']} | "
            f"{r['trade_identity']} | {r['identity_status']} | "
            f"{r['relevant_30d']}/{r['relevant_90d']}/{r['relevant_180d']} | "
            f"{r['primary_demand']} | {r.get('phone') or ''} | {r.get('quality_label')} |"
        )
    lines += ["", "## J. Other-lane Top 10", ""]
    for lane in ("FUEL_GAS_PROPANE", "FIRE_BACKFLOW", "CIVIL_WET_UTILITY", "GENERAL_CONTRACTOR_CM"):
        lines.append(f"### {lane}")
        for r in (books.get(lane) or [])[:10]:
            sub = f" · {r.get('fuel_gas_subtype')}" if r.get("fuel_gas_subtype") else ""
            lines.append(
                f"- {r['presentation_rank']}. {r['canonical_name']} ({r['account_priority_score']}){sub} — {r['sales_why_now'][:160]}"
            )
        lines.append("")
    lines += ["## K. Known-company membership", ""]
    for k in known:
        lane_s = ", ".join(
            f"{x['lane_key']}:{x['fit']}" + (f"/{x['subtype']}" if x.get("subtype") else "")
            for x in k["lanes"]
        )
        lines.append(f"- {k['display_name']} (`{k['company_id']}`) pri={k['account_priority_score']} · {lane_s}")
    lines += [
        "",
        f"## N/O. Internal HTML prototype: `{html_path}`",
        "",
        "## Q. Guardrails",
        "",
        f"- elapsed: {elapsed_s}s",
        f"- unchanged: {before_guard == after_guard}",
        f"- before: {before_guard}",
        f"- after: {after_guard}",
        "",
        "STOP. No production/customer-facing cutover.",
        "",
        f"Snapshot: `{snap}`",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
