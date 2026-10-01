"""Freeze the current PLUMBING_CORE Top 25. Enrichment must not change this set."""

from __future__ import annotations

import json
import sqlite3

from pipeline.config.settings import SALES_LANE_VERSION, SALES_TRIAL_VERSION
from pipeline.contactability.coverage import account_contact_summary
from pipeline.db.database import now_iso
from pipeline.sales_lanes.lanes import PLUMBING_CORE


def load_plumbing_book(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM sales_lane_books
        WHERE lane_key=? AND model_version=?
        ORDER BY presentation_rank
        """,
        (PLUMBING_CORE, SALES_LANE_VERSION),
    ).fetchall()
    out = []
    for row in rows:
        rec = dict(row)
        raw = rec.get("member_company_ids")
        if isinstance(raw, str):
            rec["member_company_ids"] = json.loads(raw)
        out.append(rec)
    return out


def cohort_company_ids(rows: list[dict]) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for rec in rows:
        for cid in rec.get("member_company_ids") or [rec["primary_company_id"]]:
            cid = int(cid)
            if cid not in seen:
                seen.add(cid)
                ids.append(cid)
    return ids


def freeze_plumbing_core(conn: sqlite3.Connection) -> list[dict]:
    """Snapshot membership/order + then-current contact coverage."""
    book = load_plumbing_book(conn)
    now = now_iso()
    conn.execute(
        "DELETE FROM sales_lane_snapshots WHERE lane_key=? AND model_version=?",
        (PLUMBING_CORE, SALES_TRIAL_VERSION),
    )
    frozen = []
    for rec in book:
        summary = account_contact_summary(conn, int(rec["primary_company_id"]))
        row = {
            "lane_key": PLUMBING_CORE,
            "presentation_rank": int(rec["presentation_rank"]),
            "canonical_key": rec["canonical_key"],
            "canonical_name": rec["canonical_name"],
            "primary_company_id": int(rec["primary_company_id"]),
            "member_company_ids": rec["member_company_ids"],
            "account_priority_score": float(rec["account_priority_score"]),
            "identity_status": rec.get("identity_status"),
            "quality_label": rec.get("quality_label"),
            "phone": summary.get("phone") or rec.get("phone"),
            "email": summary.get("email") or rec.get("email"),
            "website": summary.get("website") or rec.get("website"),
            "contact_confidence": summary.get("contact_confidence"),
            "actionable": 1 if summary.get("actionable") else 0,
            "has_phone": summary.get("has_phone"),
            "has_email": summary.get("has_email"),
            "has_website": summary.get("has_website"),
            "has_named": summary.get("has_named"),
            "trade_identity": rec.get("trade_identity"),
            "relevant_30d": rec.get("relevant_30d"),
            "relevant_90d": rec.get("relevant_90d"),
            "relevant_180d": rec.get("relevant_180d"),
            "historical_relevant": rec.get("historical_relevant"),
            "likely_buy": rec.get("likely_buy"),
            "sales_why_now": rec.get("sales_why_now"),
            "strongest_project": rec.get("strongest_project"),
            "sales_lanes": rec.get("sales_lanes"),
            "primary_demand": rec.get("primary_demand"),
            "roc_status_safe": rec.get("roc_status_safe"),
        }
        conn.execute(
            """
            INSERT INTO sales_lane_snapshots (
                lane_key, presentation_rank, canonical_key, canonical_name,
                primary_company_id, member_company_ids, account_priority_score,
                identity_status, quality_label, phone, email, website,
                contact_confidence, actionable, model_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                PLUMBING_CORE,
                row["presentation_rank"],
                row["canonical_key"],
                row["canonical_name"],
                row["primary_company_id"],
                json.dumps(row["member_company_ids"]),
                row["account_priority_score"],
                row["identity_status"],
                row["quality_label"],
                row["phone"],
                row["email"],
                row["website"],
                row["contact_confidence"],
                row["actionable"],
                SALES_TRIAL_VERSION,
                now,
            ),
        )
        frozen.append(row)
    conn.commit()
    return frozen


def load_frozen(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT * FROM sales_lane_snapshots
        WHERE lane_key=? AND model_version=?
        ORDER BY presentation_rank
        """,
        (PLUMBING_CORE, SALES_TRIAL_VERSION),
    ).fetchall()
    out = []
    for row in rows:
        rec = dict(row)
        rec["member_company_ids"] = json.loads(rec["member_company_ids"])
        out.append(rec)
    return out
