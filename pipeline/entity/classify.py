"""Classify ROC duplicate candidate pairs. Recommendations only — no merges."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from pipeline.config.settings import ENTITY_MATCH_VERSION
from pipeline.db.database import now_iso
from pipeline.entity.names import (
    compact_company_name,
    distinctive_tokens,
    is_person,
    token_jaccard,
)

SAME_HIGH = "SAME_ENTITY_HIGH_CONFIDENCE"
SAME_PROB = "SAME_ENTITY_PROBABLE"
POSS_DUP = "POSSIBLE_DUPLICATE"
DISTINCT = "DISTINCT_ENTITIES"
NEEDS = "NEEDS_REVIEW"

AUTO_LINK = {SAME_HIGH, SAME_PROB}


def classify_pair(
    name_a: str | None,
    name_b: str | None,
    *,
    roc_legal: str | None = None,
    roc_dba: str | None = None,
    person_a: bool | None = None,
    person_b: bool | None = None,
) -> tuple[str, list[str]]:
    ca = compact_company_name(name_a)
    cb = compact_company_name(name_b)
    cl = compact_company_name(roc_legal)
    cd = compact_company_name(roc_dba)
    roc_keys = {k for k in (cl, cd) if k}
    a_hits = bool(ca and ca in roc_keys)
    b_hits = bool(cb and cb in roc_keys)
    pa = is_person(name_a) if person_a is None else person_a
    pb = is_person(name_b) if person_b is None else person_b
    reasons: list[str] = []

    if ca and ca == cb:
        return SAME_HIGH, ["compact_name_equal"]
    if a_hits and b_hits:
        return SAME_HIGH, ["both_match_roc_legal_or_dba"]
    if cd and (ca == cd or cb == cd):
        reasons.append("dba_matches_one_name")
        return SAME_HIGH, reasons + ["shared_license_plus_dba"]
    if cl and (ca == cl or cb == cl) and (a_hits or b_hits):
        return SAME_HIGH, ["legal_name_plus_shared_license"]

    jac = token_jaccard(name_a, name_b)
    shared = distinctive_tokens(name_a) & distinctive_tokens(name_b)
    if pa ^ pb and (a_hits or b_hits or cd):
        return SAME_PROB, ["person_record_vs_licensed_business"]
    if shared and jac >= 0.4:
        return SAME_PROB, ["shared_distinctive_tokens", f"jaccard={jac:.2f}"]

    if (a_hits or b_hits) and jac < 0.15 and not shared:
        return NEEDS, ["one_name_matches_roc_other_unexplained"]
    if not roc_keys:
        return NEEDS, ["shared_license_without_roc_name"]
    if jac == 0 and not shared:
        return NEEDS, ["disjoint_names_shared_license"]
    if jac < 0.2:
        return POSS_DUP, ["weak_name_overlap_shared_license"]
    return POSS_DUP, ["shared_license_possible_variant"]


def _company_snapshot(conn: sqlite3.Connection, company_id: int) -> dict:
    row = conn.execute(
        """
        SELECT c.id, c.display_name, c.legal_name, c.normalized_name, c.dba_name,
               c.main_phone, c.main_email, c.website, c.address_line_1, c.city,
               c.postal_code, c.license_number,
               i.total_projects, i.total_permits, i.latest_activity_date,
               p.account_priority_score, p.trade_identity, p.why_now,
               p.primary_demand_category, p.strongest_capability
        FROM companies c
        LEFT JOIN company_intelligence i ON i.company_id=c.id
        LEFT JOIN company_customer_priority p
          ON p.company_id=c.id AND p.profile_key='plumbing_supply'
        WHERE c.id=?
        """,
        (company_id,),
    ).fetchone()
    if row is None:
        return {"id": company_id}
    d = dict(row)
    caps = [
        r["capability"]
        for r in conn.execute(
            "SELECT capability FROM company_capabilities WHERE company_id=? ORDER BY confidence DESC",
            (company_id,),
        )
    ]
    d["capabilities"] = caps
    match = conn.execute(
        """
        SELECT match_status, match_confidence, match_reasons, normalized_license_number
        FROM roc_company_matches WHERE company_id=? LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    d["match_status"] = None if match is None else match["match_status"]
    d["match_confidence"] = None if match is None else match["match_confidence"]
    d["match_reasons"] = None if match is None else match["match_reasons"]
    return d


def review_duplicate_candidates(conn: sqlite3.Connection) -> dict:
    """Write entity_duplicate_reviews for every ROC duplicate pair. No merges."""
    now = now_iso()
    conn.execute("DELETE FROM entity_duplicate_reviews WHERE matching_version=?", (ENTITY_MATCH_VERSION,))
    pairs = conn.execute(
        """
        SELECT company_id_a, company_id_b, normalized_license_number,
               roc_business_name, reason, match_confidence
        FROM roc_duplicate_candidates
        ORDER BY company_id_a, company_id_b, normalized_license_number
        """
    ).fetchall()
    stats: dict[str, int] = defaultdict(int)
    for row in pairs:
        a = _company_snapshot(conn, int(row["company_id_a"]))
        b = _company_snapshot(conn, int(row["company_id_b"]))
        roc = conn.execute(
            """
            SELECT raw_business_name, raw_dba, address_line_1, city, postal_code,
                   qualifying_party, normalized_status, raw_class
            FROM roc_licenses
            WHERE normalized_license_number=?
            LIMIT 1
            """,
            (row["normalized_license_number"],),
        ).fetchone()
        roc_d = dict(roc) if roc else {}
        classification, reasons = classify_pair(
            a.get("display_name"),
            b.get("display_name"),
            roc_legal=roc_d.get("raw_business_name") or row["roc_business_name"],
            roc_dba=roc_d.get("raw_dba"),
            person_a=is_person(a.get("display_name")),
            person_b=is_person(b.get("display_name")),
        )
        evidence = {
            "reasons": reasons,
            "roc_duplicate_reason": row["reason"],
            "name_a": a.get("display_name"),
            "name_b": b.get("display_name"),
            "normalized_a": a.get("normalized_name"),
            "normalized_b": b.get("normalized_name"),
            "compact_a": compact_company_name(a.get("display_name")),
            "compact_b": compact_company_name(b.get("display_name")),
            "roc_legal": roc_d.get("raw_business_name") or row["roc_business_name"],
            "roc_dba": roc_d.get("raw_dba"),
            "address": roc_d.get("address_line_1"),
            "city": roc_d.get("city"),
            "postal_code": roc_d.get("postal_code"),
            "qualifying_party": roc_d.get("qualifying_party"),
            "snapshot_a": {
                "phone": a.get("main_phone"),
                "email": a.get("main_email"),
                "projects": a.get("total_projects"),
                "permits": a.get("total_permits"),
                "latest": a.get("latest_activity_date"),
                "capabilities": a.get("capabilities"),
                "priority": a.get("account_priority_score"),
                "trade": a.get("trade_identity"),
                "match": a.get("match_status"),
            },
            "snapshot_b": {
                "phone": b.get("main_phone"),
                "email": b.get("main_email"),
                "projects": b.get("total_projects"),
                "permits": b.get("total_permits"),
                "latest": b.get("latest_activity_date"),
                "capabilities": b.get("capabilities"),
                "priority": b.get("account_priority_score"),
                "trade": b.get("trade_identity"),
                "match": b.get("match_status"),
            },
            "do_not_merge": True,
        }
        preferred = a.get("display_name") or b.get("display_name")
        if roc_d.get("raw_dba"):
            preferred = roc_d["raw_dba"]
        elif roc_d.get("raw_business_name"):
            preferred = roc_d["raw_business_name"]
        conn.execute(
            """
            INSERT INTO entity_duplicate_reviews (
                company_id_a, company_id_b, normalized_license_number,
                classification, canonical_recommendation, evidence,
                matching_version, created_at
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                int(row["company_id_a"]),
                int(row["company_id_b"]),
                row["normalized_license_number"],
                classification,
                preferred,
                json.dumps(evidence),
                ENTITY_MATCH_VERSION,
                now,
            ),
        )
        stats[classification] += 1
    conn.commit()
    stats["pairs"] = len(pairs)
    stats["matching_version"] = ENTITY_MATCH_VERSION
    return dict(stats)
