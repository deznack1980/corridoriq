"""Read-only loaders. Every statement is a SELECT.

Recent activity is read from permits and projects directly, not from
project_customer_relevance or company_customer_priority. Those tables are
snapshots written by batch jobs, and a permit ingested after the last batch
is missing from them. The stored why-now is loaded only to compare against.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

_CHUNK = 400

try:
    from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE as _PROFILE
    from pipeline.config.settings import SALES_LANE_VERSION as _LANE_VERSION
except Exception:  # pragma: no cover - settings always importable in repo
    _PROFILE = "plumbing_supply"
    _LANE_VERSION = "sales-lane-v1"

PROFILE_KEY = _PROFILE
LANE_VERSION = _LANE_VERSION


def _safe(conn: sqlite3.Connection, sql: str, params: tuple | list = ()) -> list[dict]:
    try:
        return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
    except sqlite3.Error:
        return []


def _by_ids(conn: sqlite3.Connection, sql: str, ids: list[int], extra: tuple = ()) -> list[dict]:
    """sql contains `{ids}` where the IN list goes. extra params come after the ids."""
    out: list[dict] = []
    for i in range(0, len(ids), _CHUNK):
        chunk = ids[i:i + _CHUNK]
        marks = ",".join("?" * len(chunk))
        out.extend(_safe(conn, sql.format(ids=marks), (*chunk, *extra)))
    return out


def recent_permits(
    conn: sqlite3.Connection,
    *,
    since: str,
    until: str,
    company_ids: list[int] | None = None,
    attributed: bool = True,
) -> list[dict]:
    """Permits whose observed issued (or filed) day falls in [since, until]."""
    cols = """
        p.id AS permit_id, p.jurisdiction, p.permit_number, p.permit_type, p.permit_subtype,
        p.status, p.description, p.project_description, p.issued_date, p.filed_date,
        p.job_address, p.city, p.zip, p.general_contractor_name, p.plumbing_contractor_name,
        p.permit_url, p.first_seen_at, p.last_updated_at,
        pr.id AS project_id, pr.contractor_company_id AS company_id,
        pr.project_category, pr.opportunity_date, pr.opportunity_date_basis
    """
    day = "substr(COALESCE(NULLIF(TRIM(p.issued_date), ''), p.filed_date), 1, 10)"
    if company_ids is not None:
        return _by_ids(
            conn,
            f"""
            SELECT {cols}
            FROM projects pr JOIN permits p ON p.id = pr.permit_id
            WHERE pr.contractor_company_id IN ({{ids}})
              AND {day} >= ? AND {day} <= ?
            """,
            company_ids,
            (since, until),
        )
    where = "pr.contractor_company_id IS NOT NULL" if attributed else "pr.contractor_company_id IS NULL"
    return _safe(
        conn,
        f"""
        SELECT {cols}
        FROM projects pr JOIN permits p ON p.id = pr.permit_id
        WHERE {where} AND {day} >= ? AND {day} <= ?
        """,
        (since, until),
    )


def company_context(conn: sqlite3.Connection, company_ids: list[int]) -> dict[int, dict]:
    ids = sorted({int(i) for i in company_ids})
    ctx: dict[int, dict] = {
        i: {
            "company": None,
            "lanes": [],
            "priority": None,
            "capabilities": [],
            "contacts": [],
            "roc": [],
            "identity_review": None,
            "overrides": [],
            "canonical": None,
            "canonical_peers": [],
            "duplicate_reviews": [],
            "name_peers": [],
        }
        for i in ids
    }
    if not ids:
        return ctx
    for r in _by_ids(
        conn,
        """
        SELECT id, display_name, legal_name, normalized_name, lifecycle_state, merged_into_id
        FROM companies WHERE id IN ({ids})
        """,
        ids,
    ):
        ctx[int(r["id"])]["company"] = r
    for r in _by_ids(
        conn,
        """
        SELECT company_id, lane_key, fit, subtype, presentable, model_version
        FROM company_sales_lanes WHERE company_id IN ({ids}) AND model_version = ?
        """,
        ids,
        (LANE_VERSION,),
    ):
        ctx[int(r["company_id"])]["lanes"].append(r)
    for r in _by_ids(
        conn,
        """
        SELECT company_id, account_priority_score, trade_identity, why_now,
               most_recent_relevant_date, primary_demand_category, relevant_30d,
               relevant_90d, attribution_role, generated_at, model_version
        FROM company_customer_priority WHERE company_id IN ({ids}) AND profile_key = ?
        """,
        ids,
        (PROFILE_KEY,),
    ):
        ctx[int(r["company_id"])]["priority"] = r
    for r in _by_ids(
        conn,
        """
        SELECT company_id, capability, confidence, attribution_role, attribution_confidence,
               capability_class, last_evidence_date
        FROM company_capabilities WHERE company_id IN ({ids})
        """,
        ids,
    ):
        ctx[int(r["company_id"])]["capabilities"].append(r)
    for r in _by_ids(
        conn,
        """
        SELECT id, company_id, contact_type, verification_status, source_family,
               public_business_contact, contact_name, title, decision_maker_class,
               verified_at, normalized_value, contact_value, is_primary
        FROM company_contact_channels
        WHERE company_id IN ({ids}) AND status = 'active'
        """,
        ids,
    ):
        ctx[int(r["company_id"])]["contacts"].append(r)
    for r in _by_ids(
        conn,
        """
        SELECT m.company_id, m.match_status, m.match_confidence, m.normalized_license_number,
               l.normalized_class, l.raw_class
        FROM roc_company_matches m
        LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
        WHERE m.company_id IN ({ids})
        """,
        ids,
    ):
        ctx[int(r["company_id"])]["roc"].append(r)
    for r in _by_ids(
        conn,
        """
        SELECT company_id, recommended_identity_status, legal_entity, dba, remaining_conflicts,
               model_version, created_at
        FROM sales_identity_reviews WHERE company_id IN ({ids})
        ORDER BY created_at
        """,
        ids,
    ):
        ctx[int(r["company_id"])]["identity_review"] = r
    for r in _by_ids(
        conn,
        """
        SELECT company_id, original_match_status, recommended_match_status, applied, matching_version
        FROM entity_match_overrides WHERE company_id IN ({ids})
        """,
        ids,
    ):
        ctx[int(r["company_id"])]["overrides"].append(r)

    links = _by_ids(
        conn,
        """
        SELECT l.raw_company_id, l.canonical_company_id, l.relationship_type, l.match_confidence,
               l.reviewed_at, c.canonical_name
        FROM company_entity_links l
        LEFT JOIN canonical_companies c ON c.id = l.canonical_company_id
        WHERE l.raw_company_id IN ({ids})
        """,
        ids,
    )
    canon_ids = sorted({int(r["canonical_company_id"]) for r in links})
    members: dict[int, list[int]] = defaultdict(list)
    for r in _by_ids(
        conn,
        "SELECT raw_company_id, canonical_company_id FROM company_entity_links WHERE canonical_company_id IN ({ids})",
        canon_ids,
    ):
        members[int(r["canonical_company_id"])].append(int(r["raw_company_id"]))
    for r in links:
        cid = int(r["raw_company_id"])
        ctx[cid]["canonical"] = r
        ctx[cid]["canonical_peers"] = sorted(
            {p for p in members.get(int(r["canonical_company_id"]), []) if p != cid}
        )

    dup_sql = """
        SELECT company_id_a, company_id_b, classification, canonical_recommendation
        FROM entity_duplicate_reviews WHERE {side} IN ({{ids}})
    """
    for r in _by_ids(conn, dup_sql.format(side="company_id_a"), ids) + _by_ids(
        conn, dup_sql.format(side="company_id_b"), ids
    ):
        for side in ("company_id_a", "company_id_b"):
            cid = int(r[side])
            if cid in ctx and r not in ctx[cid]["duplicate_reviews"]:
                ctx[cid]["duplicate_reviews"].append(r)
    return ctx


def name_peer_index(conn: sqlite3.Connection) -> dict[str, list[int]]:
    """compact name -> active company ids. Used to flag, never to merge."""
    try:
        from pipeline.entity.names import compact_company_name
    except Exception:  # pragma: no cover
        return {}
    index: dict[str, list[int]] = defaultdict(list)
    for r in _safe(
        conn,
        """
        SELECT id, display_name FROM companies
        WHERE lifecycle_state = 'active' OR lifecycle_state IS NULL
        """,
    ):
        key = compact_company_name(r["display_name"])
        if key:
            index[key].append(int(r["id"]))
    return dict(index)


def attach_name_peers(ctx: dict[int, dict], index: dict[str, list[int]]) -> None:
    if not index:
        return
    from pipeline.entity.names import compact_company_name

    for cid, item in ctx.items():
        company = item.get("company") or {}
        key = compact_company_name(company.get("display_name"))
        item["name_peers"] = sorted(p for p in index.get(key, []) if p != cid)
