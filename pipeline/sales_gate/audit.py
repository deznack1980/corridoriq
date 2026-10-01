"""Top-25 sales-readiness audit. Does not write ranking or score columns."""

from __future__ import annotations

import json
import sqlite3

from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE, SALES_GATE_VERSION
from pipeline.contactability.coverage import account_contact_summary
from pipeline.db.database import now_iso
from pipeline.relevance.account_report import top_accounts
from pipeline.sales_gate.evidence import (
    demand_counts,
    demand_summary,
    sales_why_now,
    segment_for,
)
from pipeline.sales_gate.identity import identity_status_for
from pipeline.sales_gate.judgments import JUDGMENTS
from pipeline.sales_gate.persons import classify_person_record

GENERIC_WHY = {
    "high-value plumbing prospect",
    "strong opportunity",
    "good company to call",
}


def _roc_summary(conn: sqlite3.Connection, company_id: int) -> dict:
    rows = [
        dict(r)
        for r in conn.execute(
            """
            SELECT m.match_status, l.normalized_license_number, l.raw_class,
                   l.normalized_status, l.raw_business_name, l.raw_dba
            FROM roc_company_matches m
            LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
            WHERE m.company_id=?
            """,
            (company_id,),
        )
    ]
    classes = []
    statuses = []
    match = None
    for r in rows:
        match = match or r.get("match_status")
        if r.get("raw_class"):
            classes.append(str(r["raw_class"]))
        if r.get("normalized_status"):
            statuses.append(str(r["normalized_status"]))
    return {
        "match_status": match,
        "classes": "; ".join(dict.fromkeys(classes)),
        "status_class": (
            f"{(statuses[0] if statuses else 'none')}"
            + (f" / {'; '.join(dict.fromkeys(classes))}" if classes else "")
        ),
        "rows": rows,
    }


def _strongest_project(conn: sqlite3.Connection, company_id: int) -> dict:
    row = conn.execute(
        """
        SELECT pr.opportunity_date, pr.opportunity_score, r.relevance_score,
               COALESCE(p.project_description, p.description, '') AS description,
               p.permit_number, p.job_address, p.city
        FROM project_customer_relevance r
        JOIN projects pr ON pr.id = r.project_id
        JOIN permits p ON p.id = pr.permit_id
        WHERE r.profile_key=? AND r.company_id=?
        ORDER BY pr.opportunity_date DESC, r.relevance_score DESC
        LIMIT 1
        """,
        (CUSTOMER_RELEVANCE_PROFILE, company_id),
    ).fetchone()
    return dict(row) if row else {}


def _why_quality(text: str) -> bool:
    low = (text or "").strip().lower()
    if not low or low in GENERIC_WHY:
        return False
    return any(tok in low for tok in ("permit", "job", "license", "tank", "gas", "plumb", "backflow", "sewer", "hydrant", "days"))


def audit_account(conn: sqlite3.Connection, acct: dict) -> dict:
    cid = int(acct["company_id"])
    name = acct.get("display_name") or ""
    contact = account_contact_summary(conn, cid)
    roc = _roc_summary(conn, cid)
    identity = identity_status_for(conn, cid, roc["match_status"])
    counts = demand_counts(acct.get("demand_mix") or acct.get("demand_categories"))
    demand = demand_summary(counts, trade_identity=acct.get("trade_identity"))
    strongest = _strongest_project(conn, cid)
    overlay = JUDGMENTS.get(cid, {})
    person = classify_person_record(conn, cid, name)
    segment = overlay.get("segment") or segment_for(
        trade_identity=acct.get("trade_identity"),
        counts=counts,
        roc_classes=roc["classes"],
    )
    why = sales_why_now(
        display_name=name,
        trade_identity=acct.get("trade_identity"),
        n30=int(acct.get("relevant_30d") or 0),
        n90=int(acct.get("relevant_90d") or 0),
        n180=int(acct.get("relevant_180d") or 0),
        n_rel=int(acct.get("active_relevant_project_count") or 0),
        n_gas=int(acct.get("fuel_gas_project_count") or 0),
        n_plum=int(acct.get("plumbing_project_count") or 0),
        counts=counts,
        strongest_desc=strongest.get("description"),
        strongest_date=strongest.get("opportunity_date"),
        roc_class=roc["classes"] or None,
        identity_status=identity,
    )
    readiness = overlay.get("readiness")
    if person["person_class"] not in {"not_person_name"} and person["sales_readiness"] == "NOT_SALES_READY":
        readiness = "NOT_SALES_READY"
    if not readiness:
        if not contact["actionable"] and identity in {"UNRESOLVED", "CONFLICT", "POSSIBLE"}:
            readiness = "RESEARCH_FIRST"
        elif contact["actionable"]:
            readiness = "SALES_READY_WITH_CAUTION"
        else:
            readiness = "NOT_SALES_READY"
    fulfillment = overlay.get("fulfillment")
    if readiness == "NOT_SALES_READY":
        fulfillment = "NOT_READY"
    elif not fulfillment:
        if identity in {"UNRESOLVED", "CONFLICT"} and cid not in JUDGMENTS:
            fulfillment = "IDENTITY_UNRESOLVED"
        elif contact["actionable"] and demand["evidence_level"] != "INSUFFICIENT_EVIDENCE":
            fulfillment = "READY_FOR_OUTREACH"
        elif contact["actionable"]:
            fulfillment = "CONTACTABLE_BUT_DEMAND_UNCLEAR"
        elif demand["evidence_level"] != "INSUFFICIENT_EVIDENCE":
            fulfillment = "DEMAND_CLEAR_BUT_CONTACT_WEAK"
        else:
            fulfillment = "NOT_READY"
    intel_who = identity in {"VERIFIED", "HIGH_CONFIDENCE"} or (
        person["person_class"] == "not_person_name" and readiness != "NOT_SALES_READY"
    )
    intel_why = _why_quality(why)
    intel_what = demand["evidence_level"] != "INSUFFICIENT_EVIDENCE"
    intel_how = bool(contact["actionable"])
    intel_proj = bool(strongest.get("description") or strongest.get("permit_number"))
    flags = list(overlay.get("flags") or [])
    if person["person_class"] not in {"not_person_name"}:
        flags.append(f"person:{person['person_class']}")
    action = overlay.get("action") or (
        "Call the published business line and ask for purchasing or the estimator."
        if contact["actionable"]
        else "No useful public channel; do not hunt personal numbers."
    )
    strongest_text = ""
    if strongest:
        strongest_text = " | ".join(
            x
            for x in (
                (strongest.get("opportunity_date") or "")[:10],
                strongest.get("permit_number"),
                (strongest.get("city") or ""),
                (strongest.get("description") or "")[:160].replace("\n", " "),
            )
            if x
        )
    return {
        "company_id": cid,
        "rank": acct.get("rank"),
        "display_name": name,
        "account_priority_score": float(acct.get("account_priority_score") or 0),
        "identity_status": identity,
        "roc_status_class": roc["status_class"],
        "contractor_trade_identity": acct.get("trade_identity"),
        "account_segment": segment,
        "primary_demand_category": demand["primary"] or acct.get("primary_demand_category"),
        "secondary_demand_categories": json.dumps(demand["secondary"]),
        "relevant_30d": int(acct.get("relevant_30d") or 0),
        "relevant_90d": int(acct.get("relevant_90d") or 0),
        "relevant_180d": int(acct.get("relevant_180d") or 0),
        "strongest_project": strongest_text,
        "phone": contact.get("phone"),
        "email": contact.get("email"),
        "website": contact.get("website"),
        "named_contact": contact.get("contact_name"),
        "contact_role": contact.get("role"),
        "contact_confidence": contact.get("contact_confidence"),
        "sales_why_now": why,
        "salesperson_action": action,
        "sales_readiness": readiness,
        "demand_evidence_level": demand["evidence_level"],
        "likely_buy": demand["likely_buy"],
        "fulfillment_readiness": fulfillment,
        "intel_who": int(intel_who),
        "intel_why": int(intel_why),
        "intel_what": int(intel_what),
        "intel_how": int(intel_how),
        "intel_projects": int(intel_proj),
        "flags": json.dumps(flags),
        "has_phone": contact["has_phone"],
        "has_email": contact["has_email"],
        "has_website": contact["has_website"],
        "has_named": contact["has_named"],
        "has_useful_role": contact["has_useful_role"],
        "actionable": contact["actionable"],
        "why_credible": intel_why,
        "all_five": all((intel_who, intel_why, intel_what, intel_how, intel_proj)),
        "person_class": person["person_class"],
    }


def write_account_reviews(conn: sqlite3.Connection, *, limit: int = 25) -> list[dict]:
    now = now_iso()
    conn.execute(
        "DELETE FROM sales_account_reviews WHERE model_version=? AND profile_key=?",
        (SALES_GATE_VERSION, CUSTOMER_RELEVANCE_PROFILE),
    )
    rows = []
    for acct in top_accounts(conn, limit=limit):
        rec = audit_account(conn, acct)
        rows.append(rec)
        conn.execute(
            """
            INSERT INTO sales_account_reviews (
                profile_key, company_id, rank_at_review, display_name,
                account_priority_score, identity_status, roc_status_class,
                contractor_trade_identity, account_segment, primary_demand_category,
                secondary_demand_categories, relevant_30d, relevant_90d, relevant_180d,
                strongest_project, phone, email, website, named_contact, contact_role,
                contact_confidence, sales_why_now, salesperson_action, sales_readiness,
                demand_evidence_level, likely_buy, fulfillment_readiness,
                intel_who, intel_why, intel_what, intel_how, intel_projects,
                flags, model_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                CUSTOMER_RELEVANCE_PROFILE,
                rec["company_id"],
                rec["rank"],
                rec["display_name"],
                rec["account_priority_score"],
                rec["identity_status"],
                rec["roc_status_class"],
                rec["contractor_trade_identity"],
                rec["account_segment"],
                rec["primary_demand_category"],
                rec["secondary_demand_categories"],
                rec["relevant_30d"],
                rec["relevant_90d"],
                rec["relevant_180d"],
                rec["strongest_project"],
                rec["phone"],
                rec["email"],
                rec["website"],
                rec["named_contact"],
                rec["contact_role"],
                rec["contact_confidence"],
                rec["sales_why_now"],
                rec["salesperson_action"],
                rec["sales_readiness"],
                rec["demand_evidence_level"],
                rec["likely_buy"],
                rec["fulfillment_readiness"],
                rec["intel_who"],
                rec["intel_why"],
                rec["intel_what"],
                rec["intel_how"],
                rec["intel_projects"],
                rec["flags"],
                SALES_GATE_VERSION,
                now,
            ),
        )
    conn.commit()
    return rows


def scorecard(rows: list[dict]) -> dict:
    n = len(rows) or 1
    def pct(pred) -> float:
        return round(100.0 * sum(1 for r in rows if pred(r)) / n, 1)

    readiness = {}
    segments = {}
    for r in rows:
        readiness[r["sales_readiness"]] = readiness.get(r["sales_readiness"], 0) + 1
        segments[r["account_segment"]] = segments.get(r["account_segment"], 0) + 1
    return {
        "n": len(rows),
        "readiness": readiness,
        "segments": segments,
        "pct_sales_ready": pct(lambda r: r["sales_readiness"] == "SALES_READY"),
        "pct_caution": pct(lambda r: r["sales_readiness"] == "SALES_READY_WITH_CAUTION"),
        "pct_research": pct(lambda r: r["sales_readiness"] == "RESEARCH_FIRST"),
        "pct_not_ready": pct(lambda r: r["sales_readiness"] == "NOT_SALES_READY"),
        "pct_identity_high": pct(lambda r: r["identity_status"] in {"VERIFIED", "HIGH_CONFIDENCE"}),
        "pct_phone": pct(lambda r: r["has_phone"]),
        "pct_email": pct(lambda r: r["has_email"]),
        "pct_website": pct(lambda r: r["has_website"]),
        "pct_named": pct(lambda r: r["has_named"]),
        "pct_useful_role": pct(lambda r: r["has_useful_role"]),
        "pct_why": pct(lambda r: r["why_credible"]),
        "pct_demand": pct(lambda r: r["demand_evidence_level"] != "INSUFFICIENT_EVIDENCE"),
        "pct_fulfillment": pct(lambda r: r["fulfillment_readiness"] == "READY_FOR_OUTREACH"),
        "pct_all_five": pct(lambda r: r["all_five"]),
        "immediately_callable": [
            r for r in rows if r["sales_readiness"] == "SALES_READY" and r["actionable"]
        ],
        "identity_first": [
            r for r in rows if r["identity_status"] in {"POSSIBLE", "CONFLICT", "UNRESOLVED"}
            and r["sales_readiness"] != "NOT_SALES_READY"
        ],
        "no_contact": [r for r in rows if not r["actionable"]],
        "plumbing_gas_wet": [
            r
            for r in rows
            if r["account_segment"]
            in {"RESIDENTIAL_PLUMBING", "COMMERCIAL_PLUMBING", "FUEL_GAS_PROPANE", "MULTI_TRADE", "HVAC_MECHANICAL"}
        ],
        "other_segment": [
            r
            for r in rows
            if r["account_segment"]
            in {"FIRE_BACKFLOW", "GENERAL_CONTRACTOR_CM", "CIVIL_WET_UTILITY", "SPECIALTY_OTHER"}
        ],
        "should_not_occupy": [
            r
            for r in rows
            if r["sales_readiness"] == "NOT_SALES_READY"
            or r["account_segment"] in {"GENERAL_CONTRACTOR_CM", "SPECIALTY_OTHER", "UNKNOWN"}
            or any(
                token in (r.get("flags") or "")
                for token in (
                    "duplicate_of_47553",
                    "duplicate_of_43066",
                    "wrong_trade_slot",
                    "retail_exchange_cages",
                    "permit_expeditor",
                    "person_name",
                )
            )
        ],
        "all_five_rows": [r for r in rows if r["all_five"]],
        "missing_intel": [r for r in rows if not r["all_five"]],
    }
