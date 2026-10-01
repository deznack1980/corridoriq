"""Read-only commercial data-trust audit. Diagnosis only.

Every statement is a SELECT. The connection is opened with mode=ro and
PRAGMA query_only. This module does not merge, score, classify, or write
to CorridorIQ operational tables.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agents.ceo.operating_state.collector import connect_readonly, resolve_db_path

SELECTS: dict[str, str] = {
    "jurisdiction_coverage": """
        SELECT j.slug, j.name, j.status,
               COALESCE(p.n, 0) AS permits,
               p.oldest_issued, p.newest_issued, p.last_touch,
               p.no_address, p.no_valuation, p.no_contractor_text,
               p.blank_permit_number
        FROM jurisdictions j
        LEFT JOIN (
            SELECT jurisdiction,
                   COUNT(*) AS n,
                   MIN(issued_date) AS oldest_issued,
                   MAX(issued_date) AS newest_issued,
                   MAX(last_updated_at) AS last_touch,
                   SUM(CASE WHEN job_address IS NULL OR TRIM(job_address) = '' THEN 1 ELSE 0 END) AS no_address,
                   SUM(CASE WHEN valuation IS NULL OR valuation = 0 THEN 1 ELSE 0 END) AS no_valuation,
                   SUM(CASE WHEN COALESCE(TRIM(plumbing_contractor_name), '') = ''
                             AND COALESCE(TRIM(general_contractor_name), '') = ''
                        THEN 1 ELSE 0 END) AS no_contractor_text,
                   SUM(CASE WHEN permit_number IS NULL OR TRIM(permit_number) = '' THEN 1 ELSE 0 END) AS blank_permit_number
            FROM permits
            GROUP BY jurisdiction
        ) p ON p.jurisdiction = j.slug
        ORDER BY j.status, permits DESC
    """,
    "source_health": """
        SELECT jurisdiction_slug, health_state, consecutive_failures,
               last_success_at, last_data_at, newest_source_date,
               days_since_newest_source, days_since_last_data,
               last_error_type, runs_7d, failures_7d
        FROM source_health_snapshot
        WHERE captured_at = (SELECT MAX(captured_at) FROM source_health_snapshot)
        ORDER BY jurisdiction_slug
    """,
    "latest_pipeline_runs": """
        SELECT run_type, status, started_at, completed_at,
               jurisdictions_attempted, jurisdictions_succeeded, jurisdictions_failed,
               records_received
        FROM pipeline_runs
        ORDER BY id DESC
        LIMIT 5
    """,
    "permit_project_link": """
        SELECT
            (SELECT COUNT(*) FROM permits) AS permits,
            (SELECT COUNT(*) FROM projects) AS projects,
            (SELECT COUNT(*) FROM permits pe LEFT JOIN projects pr ON pr.permit_id = pe.id WHERE pr.id IS NULL) AS permits_without_project,
            (SELECT COUNT(*) FROM projects pr LEFT JOIN permits pe ON pe.id = pr.permit_id WHERE pe.id IS NULL) AS projects_without_permit
    """,
    "permit_date_anomalies": """
        SELECT
            SUM(CASE WHEN issued_date IS NULL OR TRIM(issued_date) = '' THEN 1 ELSE 0 END) AS missing_issued,
            SUM(CASE WHEN issued_date LIKE '____-__-__%' AND issued_date > date('now', '+60 days') THEN 1 ELSE 0 END) AS future_issued,
            SUM(CASE WHEN issued_date LIKE '____-__-__%' AND issued_date < '1990-01-01' THEN 1 ELSE 0 END) AS issued_before_1990,
            SUM(CASE WHEN issued_date IS NOT NULL AND TRIM(issued_date) != '' AND issued_date NOT LIKE '____-__-__%' THEN 1 ELSE 0 END) AS non_iso_issued
        FROM permits
    """,
    "project_quality": """
        SELECT
            COUNT(*) AS projects,
            SUM(CASE WHEN contractor_company_id IS NULL THEN 1 ELSE 0 END) AS no_contractor_company,
            SUM(CASE WHEN project_category IS NULL OR TRIM(project_category) = '' THEN 1 ELSE 0 END) AS no_category,
            SUM(CASE WHEN opportunity_date IS NULL OR TRIM(opportunity_date) = '' THEN 1 ELSE 0 END) AS no_opportunity_date,
            SUM(CASE WHEN valuation_missing = 1 THEN 1 ELSE 0 END) AS no_valuation
        FROM (
            SELECT pr.contractor_company_id, pr.project_category, pr.opportunity_date,
                   CASE WHEN pe.valuation IS NULL OR pe.valuation = 0 THEN 1 ELSE 0 END AS valuation_missing
            FROM projects pr
            LEFT JOIN permits pe ON pe.id = pr.permit_id
        )
    """,
    "project_categories": """
        SELECT COALESCE(project_category, '(blank)') AS project_category, COUNT(*) AS n
        FROM projects
        GROUP BY 1
        ORDER BY n DESC
        LIMIT 15
    """,
    "project_lifecycle": """
        SELECT COALESCE(project_lifecycle, '(blank)') AS project_lifecycle, COUNT(*) AS n
        FROM projects
        GROUP BY 1
        ORDER BY n DESC
    """,
    "occupancy": """
        SELECT
            SUM(CASE WHEN occupancy_type IS NULL OR TRIM(occupancy_type) = '' THEN 1 ELSE 0 END) AS blank,
            SUM(CASE WHEN LOWER(occupancy_type) LIKE '%res%' THEN 1 ELSE 0 END) AS residential_like,
            SUM(CASE WHEN LOWER(occupancy_type) LIKE '%com%' THEN 1 ELSE 0 END) AS commercial_like,
            COUNT(*) AS permits
        FROM permits
    """,
    "attribution_roles": """
        SELECT COALESCE(attribution_role, '(blank)') AS attribution_role,
               COALESCE(capability_class, '(blank)') AS capability_class,
               COUNT(*) AS companies
        FROM company_capabilities
        GROUP BY 1, 2
        ORDER BY companies DESC
    """,
    "capability_names": """
        SELECT capability, COUNT(*) AS companies
        FROM company_capabilities
        GROUP BY 1
        ORDER BY companies DESC
    """,
    "company_identity": """
        SELECT
            COUNT(*) AS companies,
            SUM(CASE WHEN lifecycle_state = 'active' THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN lifecycle_state = 'merged' OR merged_into_id IS NOT NULL THEN 1 ELSE 0 END) AS merged_or_pointer,
            (SELECT COUNT(*) FROM canonical_companies) AS canonical_rows,
            (SELECT COUNT(DISTINCT raw_company_id) FROM company_entity_links) AS linked_raw_companies,
            (SELECT COUNT(*) FROM company_aliases) AS alias_rows
        FROM companies
    """,
    "duplicate_active_names": """
        SELECT COUNT(*) AS name_groups
        FROM (
            SELECT normalized_name
            FROM companies
            WHERE lifecycle_state = 'active'
            GROUP BY normalized_name
            HAVING COUNT(*) > 1
        )
    """,
    "roc_match_status": """
        SELECT match_status, COUNT(*) AS n
        FROM roc_company_matches
        GROUP BY 1
        ORDER BY n DESC
    """,
    "roc_validation": """
        SELECT validation_result, COUNT(*) AS n
        FROM roc_identity_validations
        GROUP BY 1
        ORDER BY n DESC
    """,
    "shared_license_groups": """
        SELECT COUNT(*) AS groups_with_multiple_companies
        FROM (
            SELECT normalized_license_number
            FROM roc_company_matches
            WHERE normalized_license_number IS NOT NULL
              AND TRIM(normalized_license_number) != ''
            GROUP BY normalized_license_number
            HAVING COUNT(DISTINCT company_id) > 1
        )
    """,
    "trade_identity": """
        SELECT COALESCE(trade_identity, '(blank)') AS trade_identity, COUNT(*) AS accounts
        FROM company_customer_priority
        GROUP BY 1
        ORDER BY accounts DESC
    """,
    "demand_categories": """
        SELECT COALESCE(primary_demand_category, '(blank)') AS primary_demand_category,
               COUNT(*) AS accounts
        FROM company_customer_priority
        GROUP BY 1
        ORDER BY accounts DESC
    """,
    "recency": """
        SELECT
            COUNT(*) AS priority_accounts,
            SUM(CASE WHEN relevant_30d > 0 THEN 1 ELSE 0 END) AS active_30d,
            SUM(CASE WHEN relevant_90d > 0 THEN 1 ELSE 0 END) AS active_90d,
            SUM(CASE WHEN relevant_180d > 0 THEN 1 ELSE 0 END) AS active_180d,
            SUM(CASE WHEN relevant_30d = 0 AND relevant_90d = 0 AND relevant_180d = 0 THEN 1 ELSE 0 END) AS no_activity_180d,
            SUM(CASE WHEN account_priority_score >= 70 AND relevant_90d = 0 THEN 1 ELSE 0 END) AS score_70_plus_quiet_90d
        FROM company_customer_priority
    """,
    "sales_lane_fit": """
        SELECT lane_key, fit, COUNT(DISTINCT company_id) AS accounts
        FROM company_sales_lanes
        WHERE model_version = (
            SELECT model_version FROM company_sales_lanes ORDER BY created_at DESC LIMIT 1
        )
        GROUP BY 1, 2
        ORDER BY 1, 2
    """,
    "contact_channels": """
        SELECT verification_status, contact_type, COUNT(*) AS channels,
               COUNT(DISTINCT company_id) AS companies
        FROM company_contact_channels
        GROUP BY 1, 2
        ORDER BY channels DESC
    """,
    "decision_maker_classes": """
        SELECT COALESCE(decision_maker_class, '(blank)') AS decision_maker_class,
               COUNT(*) AS channels
        FROM company_contact_channels
        GROUP BY 1
        ORDER BY channels DESC
    """,
    "material_rows": """
        SELECT
            COUNT(*) AS rows,
            COUNT(DISTINCT project_id) AS projects,
            SUM(CASE WHEN confidence_pct >= 80 THEN 1 ELSE 0 END) AS high_confidence,
            SUM(CASE WHEN confidence_pct >= 50 AND confidence_pct < 80 THEN 1 ELSE 0 END) AS medium_confidence,
            SUM(CASE WHEN confidence_pct < 50 THEN 1 ELSE 0 END) AS low_confidence
        FROM estimated_materials
    """,
    "enrichment_flags": """
        SELECT source_family, is_enabled FROM enrichment_source_registry ORDER BY source_family
    """,
}


TRUTH_COMPANIES = (
    "PARKER & SONS",
    "KERNS PLUMBING",
    "UMBRELLA PLUMBING",
    "ADVANCED PLUMBING AND PIPING",
)


def _all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return None if row is None else dict(row)


def _safe(conn: sqlite3.Connection, name: str, sql: str):
    try:
        if "GROUP BY" in sql.upper() or "ORDER BY" in sql.upper() or name.endswith("_list"):
            return {"status": "KNOWN", "rows": _all(conn, sql)}
        rows = _all(conn, sql)
        if len(rows) == 1 and name not in {
            "jurisdiction_coverage",
            "source_health",
            "latest_pipeline_runs",
            "project_categories",
            "project_lifecycle",
            "attribution_roles",
            "capability_names",
            "roc_match_status",
            "roc_validation",
            "trade_identity",
            "demand_categories",
            "sales_lane_fit",
            "contact_channels",
            "decision_maker_classes",
            "enrichment_flags",
        }:
            return {"status": "KNOWN", "row": rows[0]}
        return {"status": "KNOWN", "rows": rows}
    except sqlite3.Error as exc:
        return {"status": "UNKNOWN", "error": type(exc).__name__}


def truth_cards(conn: sqlite3.Connection) -> list[dict]:
    cards = []
    for needle in TRUTH_COMPANIES:
        companies = _all(
            conn,
            """
            SELECT id, display_name, legal_name, normalized_name, dba_name,
                   license_number, lifecycle_state, merged_into_id
            FROM companies
            WHERE UPPER(COALESCE(display_name, '')) LIKE ?
               OR UPPER(COALESCE(legal_name, '')) LIKE ?
               OR UPPER(COALESCE(normalized_name, '')) LIKE ?
            ORDER BY id
            LIMIT 8
            """,
            (f"%{needle}%", f"%{needle}%", f"%{needle}%"),
        )
        cards.append({"needle": needle, "matches": [_company_packet(conn, row) for row in companies]})
    return cards


def _company_packet(conn: sqlite3.Connection, company: dict) -> dict:
    company_id = company["id"]
    priority = _all(
        conn,
        """
        SELECT profile_key, account_priority_score, trade_identity,
               relevant_30d, relevant_90d, relevant_180d, relevant_365d,
               active_relevant_project_count, primary_demand_category,
               most_recent_relevant_date, why_now, identity_basis, model_version
        FROM company_customer_priority
        WHERE company_id = ?
        """,
        (company_id,),
    )
    lanes = _all(
        conn,
        """
        SELECT lane_key, fit, subtype, presentable, model_version
        FROM company_sales_lanes
        WHERE company_id = ?
        ORDER BY model_version DESC, lane_key
        """,
        (company_id,),
    )
    capabilities = _all(
        conn,
        """
        SELECT capability, confidence, evidence_count, attribution_role,
               capability_class, last_evidence_date
        FROM company_capabilities
        WHERE company_id = ?
        ORDER BY confidence DESC
        """,
        (company_id,),
    )
    contacts = _all(
        conn,
        """
        SELECT contact_type, verification_status, decision_maker_class,
               title, contact_name, source_family, discovered_at, verified_at,
               CASE WHEN contact_value IS NULL OR TRIM(contact_value) = '' THEN 0 ELSE 1 END AS value_present
        FROM company_contact_channels
        WHERE company_id = ?
        ORDER BY verification_status, contact_type
        """,
        (company_id,),
    )
    roc = _all(
        conn,
        """
        SELECT match_status, match_confidence, normalized_license_number
        FROM roc_company_matches
        WHERE company_id = ?
        """,
        (company_id,),
    )
    recent = _all(
        conn,
        """
        SELECT pe.permit_number, pe.jurisdiction, pe.issued_date, pe.status,
               substr(COALESCE(pe.description, pe.project_description, ''), 1, 180) AS description,
               pr.project_category, pr.opportunity_score
        FROM projects pr
        JOIN permits pe ON pe.id = pr.permit_id
        WHERE pr.contractor_company_id = ?
        ORDER BY pe.issued_date DESC
        LIMIT 5
        """,
        (company_id,),
    )
    return {
        "company": company,
        "priority": priority,
        "lanes": lanes,
        "capabilities": capabilities,
        "contacts": contacts,
        "roc_matches": roc,
        "recent_projects": recent,
    }


def lane_books(conn: sqlite3.Connection) -> dict:
    try:
        version = conn.execute(
            """
            SELECT model_version FROM sales_lane_books
            ORDER BY created_at DESC LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return {"status": "UNKNOWN"}
    if version is None:
        return {"status": "UNKNOWN", "note": "no sales_lane_books"}
    model = version[0]
    rows = _all(
        conn,
        """
        SELECT lane_key, COUNT(*) AS rows,
               SUM(CASE WHEN quality_label = 'CLEARLY_BELONGS' THEN 1 ELSE 0 END) AS clearly,
               SUM(CASE WHEN quality_label = 'PROBABLY_BELONGS' THEN 1 ELSE 0 END) AS probably,
               SUM(CASE WHEN quality_label LIKE '%WRONG%' THEN 1 ELSE 0 END) AS wrong_lane,
               SUM(CASE WHEN relevant_90d > 0 THEN 1 ELSE 0 END) AS active_90d,
               SUM(CASE WHEN phone IS NOT NULL AND TRIM(phone) != '' THEN 1 ELSE 0 END) AS with_phone
        FROM sales_lane_books
        WHERE model_version = ?
        GROUP BY lane_key
        ORDER BY lane_key
        """,
        (model,),
    )
    return {"status": "KNOWN", "model_version": model, "rows": rows}


def run_audit(db_path: Path | None = None) -> dict:
    path = resolve_db_path(db_path)
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    if path is None or not path.exists():
        return {
            "generated_at": generated_at,
            "status": "UNKNOWN",
            "note": "database not available for a read-only audit",
        }
    conn = connect_readonly(path)
    try:
        sections = {name: _safe(conn, name, sql) for name, sql in SELECTS.items()}
        sections["lane_books"] = lane_books(conn)
        try:
            sections["truth_cards"] = {"status": "KNOWN", "cards": truth_cards(conn)}
        except sqlite3.Error:
            sections["truth_cards"] = {"status": "UNKNOWN"}
    finally:
        conn.close()
    return {
        "generated_at": generated_at,
        "status": "KNOWN",
        "database": "readonly",
        "sections": sections,
    }


def write_audit(db_path: Path | None = None, destination: Path | None = None) -> dict:
    from agents.ceo.paths import ensure_output_dir

    payload = run_audit(db_path)
    root = ensure_output_dir() if destination is None else destination
    root.mkdir(parents=True, exist_ok=True)
    target = root / "data_trust_audit.json"
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return payload
