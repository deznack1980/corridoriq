"""Internal callability. Presentation only — never writes account_priority_score."""

from __future__ import annotations

import sqlite3

from pipeline.config.settings import SALES_TRIAL_VERSION
from pipeline.contactability.coverage import account_contact_summary
from pipeline.db.database import now_iso
from pipeline.sales_gate.identity import CONFLICT, POSSIBLE, UNRESOLVED
from pipeline.sales_lanes.lanes import PLUMBING_CORE
from pipeline.sales_lanes.quality import CLEARLY_WRONG, PROBABLY_WRONG

CALL_NOW = "CALL_NOW"
CALL_WITH_CAUTION = "CALL_WITH_CAUTION"
RESEARCH_MORE = "RESEARCH_MORE"
NO_ACTIONABLE_CONTACT = "NO_ACTIONABLE_CONTACT"

WEAK_IDENTITY = {POSSIBLE, UNRESOLVED, CONFLICT}
WRONG_LANE = {PROBABLY_WRONG, CLEARLY_WRONG}


def classify_callability(row: dict, summary: dict) -> tuple[str, str]:
    if not summary.get("actionable"):
        return NO_ACTIONABLE_CONTACT, "No public business phone, email, website, or contact form."
    reasons = []
    ident = row.get("identity_status") or UNRESOLVED
    quality = row.get("quality_label") or ""
    if ident in WEAK_IDENTITY:
        reasons.append(f"identity={ident}")
    if quality in WRONG_LANE:
        reasons.append(f"lane={quality}")
    if summary.get("contact_confidence") == "CANDIDATE":
        reasons.append("contact=CANDIDATE_only")
    if int(row.get("relevant_180d") or 0) == 0:
        reasons.append("no_relevant_activity_180d")
    if reasons:
        return CALL_WITH_CAUTION, "; ".join(reasons)
    if not summary.get("has_phone") and not summary.get("has_email"):
        return RESEARCH_MORE, "Website or form only; confirm a live business line before calling."
    return CALL_NOW, "Verified public channel, sales-ready identity, and recent relevant demand."


def persist_callability(conn: sqlite3.Connection, frozen: list[dict]) -> list[dict]:
    now = now_iso()
    conn.execute(
        "DELETE FROM sales_callability WHERE lane_key=? AND model_version=?",
        (PLUMBING_CORE, SALES_TRIAL_VERSION),
    )
    out = []
    for rec in frozen:
        summary = account_contact_summary(conn, int(rec["primary_company_id"]))
        # Prefer a sibling member if the primary has weaker coverage.
        for cid in rec.get("member_company_ids") or []:
            alt = account_contact_summary(conn, int(cid))
            if alt.get("actionable") and (
                not summary.get("actionable")
                or (alt.get("contact_confidence") == "VERIFIED" and summary.get("contact_confidence") != "VERIFIED")
            ):
                summary = alt
        status, reasons = classify_callability(rec, summary)
        conn.execute(
            """
            INSERT INTO sales_callability (
                company_id, lane_key, presentation_rank, canonical_name,
                status, reasons, model_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                int(rec["primary_company_id"]),
                PLUMBING_CORE,
                int(rec["presentation_rank"]),
                rec["canonical_name"],
                status,
                reasons,
                SALES_TRIAL_VERSION,
                now,
            ),
        )
        merged = dict(rec)
        merged["after"] = summary
        merged["callability"] = status
        merged["callability_reasons"] = reasons
        out.append(merged)
    conn.commit()
    return out
