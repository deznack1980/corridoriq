"""Person-name account classification. No private personal contact collection."""

from __future__ import annotations

import json
import re
import sqlite3

from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE, SALES_GATE_VERSION
from pipeline.db.database import now_iso
from pipeline.relevance.account_report import top_accounts
from pipeline.roc.normalize import looks_like_person_name
from pipeline.roc.validate import classify_person_account

LICENSED_SOLE_PROP = "licensed_sole_proprietor"
DBA_ALIAS = "dba_business_alias"
QUALIFYING_PARTY = "qualifying_party_as_company"
OWNER_BUILDER = "owner_builder"
PERMIT_APPLICANT = "permit_applicant"
CONTRACTOR_EMPLOYEE = "contractor_employee"
UNRESOLVED_INDIVIDUAL = "unresolved_individual"
ENTITY_ARTIFACT = "entity_resolution_artifact"
COMPANY_FALSE_POSITIVE = "company_name_false_positive"

NOT_SALES_READY = "NOT_SALES_READY"
SALES_READY = "SALES_READY"
RESEARCH_FIRST = "RESEARCH_FIRST"

_POOL = re.compile(
    r"swimming pool|spa with natural gas|pe gas line buried|pool heater",
    re.I,
)
_SHOWER = re.compile(
    r"shower remodel|new shower|wet space with new shower|bath to shower",
    re.I,
)
_NEW_HOME = re.compile(r"new \d[\d,]* sf|new residence|2 story residence", re.I)
_TRADE_HINT = re.compile(
    r"\b(bath|remodel|plumbing|pool|spa|hvac|electric|homes?|construction)\b",
    re.I,
)


def _permit_blob(conn: sqlite3.Connection, company_id: int) -> str:
    rows = conn.execute(
        """
        SELECT COALESCE(p.project_description, '') || ' ' || COALESCE(p.description, '') AS blob
        FROM project_customer_relevance r
        JOIN projects pr ON pr.id = r.project_id
        JOIN permits p ON p.id = pr.permit_id
        WHERE r.profile_key=? AND r.company_id=?
        ORDER BY pr.opportunity_date DESC
        LIMIT 8
        """,
        (CUSTOMER_RELEVANCE_PROFILE, company_id),
    )
    return " ".join((row["blob"] or "") for row in rows)


def _roc_hits(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT m.match_status, m.match_reasons, l.raw_business_name, l.raw_dba,
                   l.normalized_business_name, l.qualifying_party
            FROM roc_company_matches m
            LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
            WHERE m.company_id=?
            """,
            (company_id,),
        )
    ]


def classify_person_record(conn: sqlite3.Connection, company_id: int, display_name: str) -> dict:
    person_like = looks_like_person_name(display_name)
    hits = _roc_hits(conn, company_id)
    roc_class = classify_person_account(display_name, hits)
    blob = _permit_blob(conn, company_id)
    notes = []

    if not person_like:
        return {
            "company_id": company_id,
            "display_name": display_name,
            "person_class": "not_person_name",
            "sales_readiness": SALES_READY,
            "evidence": {"roc_class": roc_class, "person_like": False},
            "notes": "Not a person-name record.",
        }

    if _TRADE_HINT.search(display_name or "") and roc_class == "unmatched_individual":
        person_class = COMPANY_FALSE_POSITIVE
        readiness = RESEARCH_FIRST
        notes.append(
            "looks_like_person_name fired on a trade-like company string. FLAG detector; do not retune."
        )
    elif roc_class == "licensed_sole_proprietor":
        person_class = LICENSED_SOLE_PROP
        readiness = SALES_READY
        notes.append("Licensed sole proprietor — keep; do not suppress.")
    elif roc_class == "company_alias":
        person_class = DBA_ALIAS
        readiness = RESEARCH_FIRST
        notes.append("Person string may be a DBA/alias of a licensed business.")
    elif roc_class == "qualifying_party":
        person_class = QUALIFYING_PARTY
        readiness = NOT_SALES_READY
        notes.append("Qualifying party is not automatically a sales contact.")
    elif _POOL.search(blob) and roc_class in {"unmatched_individual", "ambiguous_identity", "not_person_name"}:
        person_class = PERMIT_APPLICANT
        readiness = NOT_SALES_READY
        notes.append(
            "Mesa-style swimming-pool / PE gas-line permits with no matching ROC license. "
            "Treat as permit applicant, not a licensed contractor. No personal-number hunt."
        )
    elif _SHOWER.search(blob) and not hits:
        person_class = OWNER_BUILDER
        readiness = NOT_SALES_READY
        notes.append("Residential shower/wet-space remodel permit in a person name; likely owner-builder.")
    elif _NEW_HOME.search(blob) and not hits:
        person_class = OWNER_BUILDER
        readiness = NOT_SALES_READY
        notes.append("New-residence plumbing permit in a person name; likely homeowner/applicant.")
    elif roc_class == "unmatched_individual":
        person_class = UNRESOLVED_INDIVIDUAL
        readiness = NOT_SALES_READY
        notes.append("No ROC license and no commercial identity. NOT_SALES_READY; do not delete.")
    else:
        person_class = ENTITY_ARTIFACT
        readiness = NOT_SALES_READY
        notes.append(f"ROC person class={roc_class}. Commercially unusable until licensed identity exists.")

    return {
        "company_id": company_id,
        "display_name": display_name,
        "person_class": person_class,
        "sales_readiness": readiness,
        "evidence": {
            "roc_class": roc_class,
            "person_like": True,
            "pool_permit": bool(_POOL.search(blob)),
            "shower_permit": bool(_SHOWER.search(blob)),
            "new_home_permit": bool(_NEW_HOME.search(blob)),
            "roc_hit_count": len(hits),
        },
        "notes": " ".join(notes),
    }


def review_person_accounts(conn: sqlite3.Connection, *, limit: int = 50) -> dict:
    now = now_iso()
    conn.execute(
        "DELETE FROM sales_person_reviews WHERE model_version=?",
        (SALES_GATE_VERSION,),
    )
    top = top_accounts(conn, limit=limit)
    written = 0
    classes: dict[str, int] = {}
    for acct in top:
        name = acct.get("display_name") or ""
        if not looks_like_person_name(name):
            continue
        rec = classify_person_record(conn, int(acct["company_id"]), name)
        conn.execute(
            """
            INSERT INTO sales_person_reviews (
                company_id, display_name, person_class, sales_readiness,
                evidence, notes, model_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                rec["company_id"],
                rec["display_name"],
                rec["person_class"],
                rec["sales_readiness"],
                json.dumps(rec["evidence"]),
                rec["notes"],
                SALES_GATE_VERSION,
                now,
            ),
        )
        written += 1
        classes[rec["person_class"]] = classes.get(rec["person_class"], 0) + 1
    conn.commit()
    return {"written": written, "classes": classes}
