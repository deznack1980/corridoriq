"""Four-account internal sales trial. Learning, not a close plan."""

from __future__ import annotations

import sqlite3

from pipeline.contactability.coverage import account_contact_summary
from pipeline.sales_trial.callability import CALL_NOW, CALL_WITH_CAUTION, NO_ACTIONABLE_CONTACT

TRIAL_NEEDLES = (
    "PARKER & SONS",
    "KERNS PLUMBING",
    "UMBRELLA PLUMBING",
    "ADVANCED PLUMBING AND PIPING",
)

LEARNING_OBJECTIVES = [
    "Is the company actually buying the categories we infer from permits?",
    "Does permit/project activity correlate with purchasing timing?",
    "Is the contact we identified the right person (purchasing, estimating, operations, or owner)?",
    "Would they accept a simple material-list / supply-sheet request workflow?",
    "Who currently supplies them?",
    "What causes them to switch or add a supplier?",
    "Do they care about availability, price, delivery, speed, credit terms, specialty inventory, jobsite delivery, or emergency fulfillment?",
    "Would they send a PDF, spreadsheet, photo, handwritten list, text, or email?",
]


def _crm_status(conn: sqlite3.Connection, company_id: int) -> str | None:
    row = conn.execute(
        "SELECT relationship_status FROM crm_company_relationships WHERE company_id=? LIMIT 1",
        (company_id,),
    ).fetchone()
    return None if row is None else row["relationship_status"]


def opening_objective(name: str) -> str:
    return (
        f"Learn how {name} currently buys wet-side material, who decides, and whether a "
        "low-friction material-list workflow would help. Do not pitch a catalog."
    )


def build_trial_book(conn: sqlite3.Connection, enriched: list[dict]) -> dict:
    by_name = {str(r["canonical_name"]).upper(): r for r in enriched}
    selected = []
    replacements = []
    for needle in TRIAL_NEEDLES:
        match = None
        for rec in enriched:
            if needle in str(rec["canonical_name"]).upper():
                match = rec
                break
        if match is None:
            replacements.append({"needle": needle, "reason": "not_in_frozen_top25"})
            continue
        status = match.get("callability")
        if status == NO_ACTIONABLE_CONTACT:
            alt = next(
                (
                    r
                    for r in enriched
                    if r.get("callability") in {CALL_NOW, CALL_WITH_CAUTION}
                    and r["primary_company_id"] not in {s["primary_company_id"] for s in selected}
                    and "ABC WATER" not in str(r["canonical_name"]).upper()
                ),
                None,
            )
            replacements.append(
                {
                    "needle": needle,
                    "removed": match["canonical_name"],
                    "reason": "no_actionable_contact",
                    "replacement": None if alt is None else alt["canonical_name"],
                }
            )
            if alt:
                match = alt
            else:
                continue
        selected.append(match)

    cards = []
    for rec in selected:
        summary = rec.get("after") or account_contact_summary(conn, int(rec["primary_company_id"]))
        cards.append(
            {
                "company": rec["canonical_name"],
                "company_id": rec["primary_company_id"],
                "priority": rec["account_priority_score"],
                "callability": rec.get("callability"),
                "why_now": rec.get("sales_why_now"),
                "likely_material_demand": rec.get("likely_buy"),
                "recent_activity": {
                    "30d": rec.get("relevant_30d"),
                    "90d": rec.get("relevant_90d"),
                    "180d": rec.get("relevant_180d"),
                },
                "best_contact": {
                    "name": summary.get("contact_name"),
                    "role": summary.get("role") or summary.get("decision_class"),
                    "phone": summary.get("phone"),
                    "email": summary.get("email"),
                    "website": summary.get("website"),
                    "confidence": summary.get("contact_confidence"),
                },
                "current_project": rec.get("strongest_project"),
                "opening_objective": opening_objective(rec["canonical_name"]),
                "crm_status": _crm_status(conn, int(rec["primary_company_id"])),
                "identity_status": rec.get("identity_status"),
                "quality_label": rec.get("quality_label"),
            }
        )
    return {
        "accounts": cards,
        "replacements": replacements,
        "learning_objectives": LEARNING_OBJECTIVES,
        "kept_original_four": not replacements,
    }
