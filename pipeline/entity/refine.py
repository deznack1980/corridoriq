"""Recommend stronger ROC match status using extra legitimate evidence.

Writes entity_match_overrides only. Never updates roc_company_matches,
account_priority_score, or ranking flags. Fuzzy name similarity alone
never upgrades to VERIFIED.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from pipeline.config.settings import ENTITY_MATCH_VERSION
from pipeline.db.database import now_iso
from pipeline.entity.names import compact_company_name
from pipeline.roc.match import CONFLICT, HIGH, POSSIBLE, VERIFIED

WATCH_IDS_FALLBACK = (
    40676,  # Arizona Propane
    37857,  # Parker & Sons
    38349,  # Kerns
    47437,  # Kerns variant
    38307,  # Gas Piping Inc
    48510,  # Gas Piping Inc (2)
    42116,  # GAS PIPING INC.COM
    38803,  # Whiting Turner
    38855,
    45889,
    47553,  # R C I SYSTEMS
    43066,  # RCI SYSTEMS
    45906,  # Petra
    45895,  # Redpoint
)


def _zip5(value: str | None) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits[:5]


def _contact_zips(conn: sqlite3.Connection, company_id: int) -> set[str]:
    zips = set()
    for row in conn.execute(
        """
        SELECT contact_value FROM company_contact_channels
        WHERE company_id=? AND contact_type='business_address' AND status='active'
        """,
        (company_id,),
    ):
        z = _zip5(row["contact_value"])
        if len(z) == 5:
            zips.add(z)
    return zips


def _roc_candidates(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT m.match_status, m.match_reasons, m.match_confidence,
                   l.id AS roc_license_id, l.normalized_license_number,
                   l.raw_business_name, l.raw_dba, l.city, l.postal_code,
                   l.normalized_business_name, l.normalized_dba
            FROM roc_company_matches m
            LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
            WHERE m.company_id=?
            """,
            (company_id,),
        )
    ]


def refine_company(conn: sqlite3.Connection, company_id: int) -> dict | None:
    company = conn.execute(
        "SELECT id, display_name, normalized_name, city, postal_code FROM companies WHERE id=?",
        (company_id,),
    ).fetchone()
    if company is None:
        return None
    hits = _roc_candidates(conn, company_id)
    if not hits:
        return {
            "company_id": company_id,
            "original": None,
            "recommended": None,
            "evidence": ["no_roc_match_row"],
        }
    original = hits[0]["match_status"]
    compact = compact_company_name(company["display_name"])
    zips = _contact_zips(conn, company_id)
    city_zip = _zip5(company["postal_code"])
    if city_zip:
        zips.add(city_zip)

    licenses = defaultdict(list)
    for h in hits:
        if h.get("normalized_license_number"):
            licenses[h["normalized_license_number"]].append(h)

    geo_unique = []
    compact_unique = []
    for lic, rows in licenses.items():
        for row in rows:
            roc_compact = compact_company_name(row.get("raw_business_name"))
            dba_compact = compact_company_name(row.get("raw_dba"))
            roc_zip = _zip5(row.get("postal_code"))
            if compact and compact in {roc_compact, dba_compact} and roc_zip and roc_zip in zips:
                geo_unique.append(row)
            if compact and compact in {roc_compact, dba_compact}:
                compact_unique.append(row)

    evidence = []
    recommended = original
    # Multi-entity DBA names stay POSSIBLE unless ZIP uniquely selects one license.
    unique_lic_geo = {r["normalized_license_number"] for r in geo_unique}
    unique_lic_compact = {r["normalized_license_number"] for r in compact_unique}
    if original in {POSSIBLE, HIGH} and len(unique_lic_geo) == 1:
        recommended = VERIFIED
        evidence = ["compact_name_plus_contact_zip", "not_fuzzy"]
    elif original == POSSIBLE and len(unique_lic_compact) == 1 and len(licenses) == 1:
        recommended = HIGH
        evidence = ["compact_name_unique_license", "not_verified_without_geo"]
    elif original == CONFLICT:
        # CONFLICT stays unless names compact-equal a single license legal/DBA AND ZIP matches.
        if len(unique_lic_geo) == 1:
            recommended = HIGH
            evidence = ["conflict_resolved_by_zip_and_compact_name"]
        else:
            recommended = CONFLICT
            evidence = ["conflict_unresolved_multiple_or_mismatched_names"]
    else:
        evidence = ["insufficient_extra_evidence"]

    if original == VERIFIED:
        recommended = VERIFIED
        evidence = ["already_verified"]

    return {
        "company_id": company_id,
        "name": company["display_name"],
        "original": original,
        "recommended": recommended,
        "evidence": evidence,
        "license_count": len(licenses),
        "compact": compact,
    }


def refine_known_ambiguities(conn: sqlite3.Connection, company_ids: list[int] | None = None) -> dict:
    now = now_iso()
    ids = list(company_ids or [])
    if not ids:
        ids = list(WATCH_IDS_FALLBACK)
        for row in conn.execute(
            """
            SELECT a.company_id
            FROM company_customer_priority a
            JOIN roc_company_matches m ON m.company_id=a.company_id
            WHERE a.profile_key='plumbing_supply'
              AND m.match_status IN (?, ?)
            ORDER BY a.account_priority_score DESC
            LIMIT 100
            """,
            (POSSIBLE, CONFLICT),
        ):
            cid = int(row["company_id"])
            if cid not in ids:
                ids.append(cid)
    conn.execute("DELETE FROM entity_match_overrides WHERE matching_version=?", (ENTITY_MATCH_VERSION,))
    stats = defaultdict(int)
    results = []
    for cid in ids:
        rec = refine_company(conn, cid)
        if rec is None:
            continue
        results.append(rec)
        conn.execute(
            """
            INSERT INTO entity_match_overrides (
                company_id, original_match_status, recommended_match_status,
                evidence, matching_version, applied, created_at
            ) VALUES (?,?,?,?,?,0,?)
            """,
            (
                cid,
                rec["original"],
                rec["recommended"] or rec["original"] or "NO_MATCH",
                json.dumps(rec),
                ENTITY_MATCH_VERSION,
                now,
            ),
        )
        stats[f"{rec['original']}->{rec['recommended']}"] += 1
        if rec["recommended"] != rec["original"]:
            stats["changed"] += 1
        else:
            stats["unchanged"] += 1
    conn.commit()
    stats["reviewed"] = len(results)
    stats["applied_to_roc_matches"] = 0
    return {"stats": dict(stats), "results": results}


def effective_match_status(conn: sqlite3.Connection, company_id: int) -> str | None:
    """Override is advisory. Production roc_company_matches is unchanged."""
    row = conn.execute(
        "SELECT match_status FROM roc_company_matches WHERE company_id=? LIMIT 1",
        (company_id,),
    ).fetchone()
    return None if row is None else row["match_status"]
