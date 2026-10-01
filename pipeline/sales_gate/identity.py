"""Curated public-business identity reviews. Never mutates roc_company_matches."""

from __future__ import annotations

import json
import sqlite3

from pipeline.config.settings import SALES_GATE_VERSION
from pipeline.db.database import now_iso

VERIFIED = "VERIFIED"
HIGH = "HIGH_CONFIDENCE"
POSSIBLE = "POSSIBLE"
CONFLICT = "CONFLICT"
UNRESOLVED = "UNRESOLVED"

# Manual Phase 4E identity cases. Fuzzy name similarity is not used.
IDENTITY_CASES = {
    40676: {
        "display_name": "Arizona Propane",
        "recommended_identity_status": HIGH,
        "canonical_account": "Dawson Enterprises Inc dba Arizona Propane Co",
        "legal_entity": "Dawson Enterprises Inc",
        "dba": "Arizona Propane Co",
        "roc_licenses": "125007, 125008",
        "roc_classes": "R-37R; CR-5",
        "business_address": "17251 E Shea Blvd, Fountain Hills, AZ 85268",
        "official_website": "https://www.arizonapropane.com/",
        "official_phone": "480-990-2245",
        "match_evidence": [
            "unique_roc_legal_dawson_enterprises_inc",
            "unique_dba_arizona_propane_co_two_licenses_same_legal",
            "official_website_zip_85268_matches_roc",
            "qualifying_party_martin_lee_dawson_jr_matches_founder",
            "not_fuzzy",
        ],
        "remaining_conflicts": (
            "CorridorIQ company city is Scottsdale (permit geography), not Fountain Hills. "
            "Dual class R-37R + CR-5 is the same legal entity, not two companies. "
            "Production roc_company_matches stays POSSIBLE_MATCH."
        ),
        "notes": (
            "Do not force VERIFIED onto roc_company_matches. HIGH_CONFIDENCE is the sales "
            "identity: one legal entity, one DBA family, official site ZIP matches ROC."
        ),
    },
    37857: {
        "display_name": "PARKER & SONS, INC",
        "recommended_identity_status": HIGH,
        "canonical_account": "Environmental Conditioning LLC dba Parker and Sons",
        "legal_entity": "Environmental Conditioning LLC",
        "dba": "Parker and Sons",
        "roc_licenses": "152654, 152656, 233298, 258885, 300696, 325553",
        "roc_classes": "CR-37; CR-39; CR-10; R-37; CR-11; R-40",
        "business_address": "3636 E Anne St, Phoenix, AZ 85040 (ROC also lists 85249)",
        "official_website": "https://www.parkerandsons.com/",
        "official_phone": "602-344-9027",
        "match_evidence": [
            "dba_parker_and_sons_on_environmental_conditioning_llc",
            "cr37_license_152654_exists",
            "qp_daryl_weston_bingham_matches_published_president",
            "trademark_owner_environmental_conditioning_llc",
            "steel_erecting_318832_is_a_different_legal_name",
            "not_fuzzy",
        ],
        "remaining_conflicts": (
            "Display name PARKER & SONS, INC vs legal LLC. Parker & Sons Steel Erecting Inc "
            "(ROC 318832, Patrick Allen Parker) is a distinct company and must not be merged. "
            "Production match stays POSSIBLE_MATCH."
        ),
        "notes": (
            "CR-37 plumbing license is on the same legal entity as the consumer HVAC brand. "
            "Identity is HIGH_CONFIDENCE; trade mix is multi-trade, not plumbing-only."
        ),
    },
    45906: {
        "display_name": "PETRA CONTRACTING",
        "recommended_identity_status": HIGH,
        "canonical_account": "K C R Inc dba Petra Contracting",
        "legal_entity": "K C R Inc",
        "dba": "Petra Contracting",
        "roc_licenses": "119815, 250195",
        "roc_classes": "A; B-4",
        "business_address": "18435 W Van Buren, Goodyear, AZ 85338",
        "official_website": "https://petracontracting.com",
        "official_phone": "623-386-6194",
        "match_evidence": [
            "permit_contractor_license_119815_exact",
            "unique_dba_petra_contracting_on_kcr_inc",
            "qp_keith_daniel_riefkohl_matches_president",
            "not_fuzzy",
        ],
        "remaining_conflicts": (
            "Production match is CONFLICT because permit identity is plumbing_specialist while "
            "ROC class A is general engineering. That is a trade-label conflict, not a wrong-company "
            "conflict. Observed demand is civil water/sewer, consistent with class A."
        ),
        "notes": (
            "WHO they are is HIGH_CONFIDENCE. They are not a plumbing contractor. "
            "Do not upgrade roc_company_matches; FLAG the plumbing_specialist label."
        ),
    },
    45895: {
        "display_name": "REDPOINT CONTRACTING",
        "recommended_identity_status": HIGH,
        "canonical_account": "Action Direct LLC dba Redpoint Contracting",
        "legal_entity": "Action Direct LLC",
        "dba": "Redpoint Contracting",
        "roc_licenses": "265009",
        "roc_classes": "KA",
        "business_address": "39506 N Daisy Mountain Dr Ste 122-340, Phoenix, AZ 85086",
        "official_website": "https://redpointcontracting.com/",
        "official_phone": "602-792-0013",
        "match_evidence": [
            "unique_dba_redpoint_contracting_on_action_direct_llc",
            "license_265009_ka_matches_official_hq_zip_85086",
            "redpointe_landscaping_361967_is_a_different_spelling_and_legal",
            "not_fuzzy",
        ],
        "remaining_conflicts": (
            "Production match is CONFLICT because compact company name does not equal legal "
            "Action Direct LLC. Third-party directories have claimed license inactivity; the "
            "CorridorIQ ROC posting list still shows KA active. Rank 90, not Top 25."
        ),
        "notes": (
            "Civil/wet-utility contractor, not a plumbing-supply Top-25 account. "
            "Do not consume CONFLICT in ranking."
        ),
    },
}


def write_identity_reviews(conn: sqlite3.Connection) -> dict:
    now = now_iso()
    conn.execute(
        "DELETE FROM sales_identity_reviews WHERE model_version=?",
        (SALES_GATE_VERSION,),
    )
    written = 0
    for cid, case in IDENTITY_CASES.items():
        exists = conn.execute("SELECT id FROM companies WHERE id=?", (cid,)).fetchone()
        if exists is None:
            continue
        conn.execute(
            """
            INSERT INTO sales_identity_reviews (
                company_id, display_name, recommended_identity_status, legal_entity, dba,
                roc_licenses, roc_classes, business_address, official_website,
                official_phone, match_evidence, remaining_conflicts, notes,
                model_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                cid,
                case["display_name"],
                case["recommended_identity_status"],
                case["legal_entity"],
                case["dba"],
                case["roc_licenses"],
                case["roc_classes"],
                case["business_address"],
                case["official_website"],
                case["official_phone"],
                json.dumps(case["match_evidence"]),
                case["remaining_conflicts"],
                case["notes"],
                SALES_GATE_VERSION,
                now,
            ),
        )
        written += 1
    conn.commit()
    return {"written": written, "applied_to_roc_matches": 0}


def identity_status_for(conn: sqlite3.Connection, company_id: int, fallback: str | None) -> str:
    row = conn.execute(
        """
        SELECT recommended_identity_status FROM sales_identity_reviews
        WHERE company_id=? AND model_version=?
        """,
        (company_id, SALES_GATE_VERSION),
    ).fetchone()
    if row:
        return row["recommended_identity_status"]
    if fallback in {"VERIFIED_MATCH", VERIFIED}:
        return VERIFIED
    if fallback in {"HIGH_CONFIDENCE_MATCH", HIGH}:
        return HIGH
    if fallback in {"POSSIBLE_MATCH", POSSIBLE}:
        return POSSIBLE
    if fallback in {"CONFLICT", "CONFLICT_MATCH"}:
        return CONFLICT
    return UNRESOLVED if not fallback else fallback
