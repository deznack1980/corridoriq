"""Conservative ROC ↔ CorridorIQ company matching. Never merges."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from difflib import SequenceMatcher

from pipeline.company_resolution.normalize import looks_like_company, normalize_city
from pipeline.config.settings import ROC_MATCH_VERSION, ROC_MODEL_VERSION
from pipeline.db.database import now_iso
from pipeline.roc.normalize import (
    looks_like_person_name,
    normalize_company_name,
    normalize_person_name,
    normalize_roc_license,
)

VERIFIED = "VERIFIED_MATCH"
HIGH = "HIGH_CONFIDENCE_MATCH"
POSSIBLE = "POSSIBLE_MATCH"
CONFLICT = "CONFLICT"
NO_MATCH = "NO_MATCH"

_FUZZY_MIN = 0.92


def match_companies(conn: sqlite3.Connection) -> dict:
    now = now_iso()
    conn.execute("DELETE FROM roc_company_matches WHERE matching_version=?", (ROC_MATCH_VERSION,))
    licenses = [dict(r) for r in conn.execute("SELECT * FROM roc_licenses")]
    by_lic: dict[str, list[dict]] = defaultdict(list)
    by_name: dict[str, list[dict]] = defaultdict(list)
    by_dba: dict[str, list[dict]] = defaultdict(list)
    by_qp: dict[str, list[dict]] = defaultdict(list)
    for row in licenses:
        by_lic[row["normalized_license_number"]].append(row)
        if row["normalized_business_name"]:
            by_name[row["normalized_business_name"]].append(row)
        if row["normalized_dba"]:
            by_dba[row["normalized_dba"]].append(row)
        if row["normalized_qualifying_party"]:
            by_qp[row["normalized_qualifying_party"]].append(row)

    companies = [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, display_name, legal_name, normalized_name, dba_name,
                   license_number, city, postal_code, main_phone, main_email,
                   address_line_1, lifecycle_state
            FROM companies
            WHERE lifecycle_state='active'
            """
        )
    ]
    aliases = defaultdict(list)
    for row in conn.execute("SELECT company_id, normalized_alias FROM company_aliases"):
        aliases[int(row["company_id"])].append(row["normalized_alias"])

    rows = []
    stats = defaultdict(int)
    for company in companies:
        decided = _match_one(company, aliases.get(int(company["id"]), []), by_lic, by_name, by_dba, by_qp)
        stats[decided["status"]] += 1
        for hit in decided["hits"]:
            rows.append(
                (
                    int(company["id"]),
                    hit["roc_license_id"],
                    hit["normalized_license_number"],
                    decided["status"],
                    decided["confidence"],
                    json.dumps(decided["reasons"]),
                    ROC_MATCH_VERSION,
                    now,
                    now,
                )
            )
        if not decided["hits"] and decided["status"] == NO_MATCH:
            continue
        if not decided["hits"]:
            rows.append(
                (
                    int(company["id"]),
                    None,
                    None,
                    decided["status"],
                    decided["confidence"],
                    json.dumps(decided["reasons"]),
                    ROC_MATCH_VERSION,
                    now,
                    now,
                )
            )

    conn.executemany(
        """
        INSERT INTO roc_company_matches (
            company_id, roc_license_id, normalized_license_number,
            match_status, match_confidence, match_reasons, matching_version,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )
    _write_internal_enrichment(conn, now)
    conn.commit()
    stats["companies_considered"] = len(companies)
    stats["match_rows"] = len(rows)
    stats["matching_version"] = ROC_MATCH_VERSION
    return dict(stats)


def _match_one(company, alias_list, by_lic, by_name, by_dba, by_qp) -> dict:
    cid = int(company["id"])
    name = normalize_company_name(
        company["normalized_name"] or company["display_name"] or company["legal_name"]
    )
    dba = normalize_company_name(company["dba_name"])
    city = normalize_city(company["city"])
    zipc = (company["postal_code"] or "").strip()[:5]
    stored_lic = normalize_roc_license(company["license_number"])
    reasons: list[str] = []

    if stored_lic and stored_lic in by_lic:
        roc_rows = by_lic[stored_lic]
        roc_names = {r["normalized_business_name"] for r in roc_rows}
        if name in roc_names or dba in roc_names or any(a in roc_names for a in alias_list):
            return _hits(VERIFIED, 98.0, ["license_and_name"], roc_rows)
        overlap = max((_token_jaccard(name, n) for n in roc_names if n), default=0.0)
        if overlap >= 0.5:
            return _hits(HIGH, 90.0, ["license_name_similar"], roc_rows)
        return _hits(CONFLICT, 40.0, ["license_name_conflict"], roc_rows)

    candidates = []
    if name and name in by_name:
        candidates.extend(("name_exact", r) for r in by_name[name])
    if dba and dba in by_name:
        candidates.extend(("dba_to_legal", r) for r in by_name[dba])
    if name and name in by_dba:
        candidates.extend(("name_to_dba", r) for r in by_dba[name])
    if dba and dba in by_dba:
        candidates.extend(("dba_exact", r) for r in by_dba[dba])
    for alias in alias_list:
        if alias and alias in by_name:
            candidates.extend(("alias_exact", r) for r in by_name[alias])
        if alias and alias in by_dba:
            candidates.extend(("alias_dba", r) for r in by_dba[alias])

    person = looks_like_person_name(company["display_name"]) and not looks_like_company(company["display_name"])
    qp_key = normalize_person_name(company["display_name"])
    if person and qp_key and qp_key in by_qp:
        candidates.extend(("qualifying_party", r) for r in by_qp[qp_key])

    if not candidates:
        return {"status": NO_MATCH, "confidence": 0.0, "reasons": ["no_candidate"], "hits": []}

    grouped: dict[str, list[tuple[str, dict]]] = defaultdict(list)
    for reason, row in candidates:
        grouped[row["normalized_license_number"]].append((reason, row))

    unique_names = {row["normalized_business_name"] for _, row in candidates}
    geo_hits = []
    for _, row in candidates:
        if city and row.get("city") and city == row["city"]:
            geo_hits.append(row)
        elif zipc and row.get("postal_code") and zipc == str(row["postal_code"])[:5]:
            geo_hits.append(row)

    roc_rows = [row for _, row in candidates]
    reason_set = sorted({r for r, _ in candidates})

    if len(unique_names) > 1 and not geo_hits:
        return _hits(CONFLICT, 45.0, ["multiple_roc_entities"] + reason_set, roc_rows)
    if geo_hits and ( "name_exact" in reason_set or "alias_exact" in reason_set):
        return _hits(VERIFIED, 96.0, ["name_plus_geo"] + reason_set, geo_hits)
    if "name_exact" in reason_set and len(unique_names) == 1:
        return _hits(HIGH, 88.0, reason_set, roc_rows)
    if "dba_exact" in reason_set or "name_to_dba" in reason_set or "dba_to_legal" in reason_set:
        status = HIGH if geo_hits else POSSIBLE
        conf = 84.0 if geo_hits else 72.0
        return _hits(status, conf, reason_set, roc_rows)
    if "qualifying_party" in reason_set:
        return _hits(POSSIBLE, 68.0, reason_set, roc_rows)
    return _hits(POSSIBLE, 62.0, reason_set, roc_rows)


def fuzzy_possible(name_a: str, name_b: str, *, city_match: bool) -> bool:
    """Fuzzy similarity is never verified. Extra geo evidence may yield POSSIBLE."""
    if not name_a or not name_b or not city_match:
        return False
    if name_a == name_b:
        return False
    return SequenceMatcher(None, name_a, name_b).ratio() >= _FUZZY_MIN


def _token_jaccard(a: str, b: str) -> float:
    sa = {t for t in (a or "").split() if len(t) > 2}
    sb = {t for t in (b or "").split() if len(t) > 2}
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _hits(status: str, confidence: float, reasons: list[str], roc_rows: list[dict]) -> dict:
    uniq = {}
    for row in roc_rows:
        uniq[row["id"]] = row
    return {
        "status": status,
        "confidence": confidence,
        "reasons": reasons,
        "hits": [
            {
                "roc_license_id": row["id"],
                "normalized_license_number": row["normalized_license_number"],
            }
            for row in uniq.values()
        ],
    }


def _write_internal_enrichment(conn: sqlite3.Connection, now: str) -> None:
    """Store matched ROC payloads internally. Ranking flag stays disabled."""
    conn.execute("DELETE FROM company_enrichment WHERE source_family='roc'")
    rows = []
    for row in conn.execute(
        """
        SELECT m.company_id, m.match_status, m.match_confidence, m.match_reasons,
               l.source_record_key, l.normalized_license_number, l.raw_business_name,
               l.raw_class, l.raw_status, l.corridor_capability, l.qualifying_party,
               l.address_line_1, l.city, l.postal_code, l.retrieved_at, l.source_url
        FROM roc_company_matches m
        JOIN roc_licenses l ON l.id = m.roc_license_id
        WHERE m.matching_version=? AND m.match_status IN (?, ?)
        """,
        (ROC_MATCH_VERSION, VERIFIED, HIGH),
    ):
        payload = {
            "license_number": row["normalized_license_number"],
            "business_name": row["raw_business_name"],
            "classification": row["raw_class"],
            "status": row["raw_status"],
            "capability": row["corridor_capability"],
            "qualifying_party": row["qualifying_party"],
            "address": row["address_line_1"],
            "city": row["city"],
            "postal_code": row["postal_code"],
            "match_status": row["match_status"],
            "source_url": row["source_url"],
            "retrieved_at": row["retrieved_at"],
        }
        rows.append(
            (
                int(row["company_id"]),
                "roc",
                row["source_record_key"],
                json.dumps(payload),
                row["match_confidence"],
                row["retrieved_at"],
                ROC_MODEL_VERSION,
                now,
                now,
            )
        )
    if rows:
        conn.executemany(
            """
            INSERT INTO company_enrichment (
                company_id, source_family, source_record_key, payload_json,
                confidence, observed_at, model_version, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            rows,
        )

