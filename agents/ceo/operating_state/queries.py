"""Read-only SQL used by the operating-state collector.

Every statement must be a SELECT. The collector opens SQLite with mode=ro
and PRAGMA query_only. It must not import get_connection.
"""

from __future__ import annotations

SELECTS: dict[str, str] = {
    "project_count": "SELECT COUNT(*) AS n FROM projects",
    "permit_count": "SELECT COUNT(*) AS n FROM permits",
    "company_count": "SELECT COUNT(*) AS n FROM companies",
    "relevance_rows": "SELECT COUNT(*) AS n FROM project_customer_relevance",
    "priority_rows": "SELECT COUNT(*) AS n FROM company_customer_priority",
    "crm_relationships": "SELECT COUNT(*) AS n FROM crm_company_relationships",
    "contractor_intel_companies": (
        "SELECT COUNT(DISTINCT company_id) AS n FROM company_capabilities"
    ),
    "roc_enabled": (
        "SELECT is_enabled AS n FROM enrichment_source_registry "
        "WHERE source_family = 'roc'"
    ),
    "latest_pipeline_run": (
        "SELECT status, started_at, completed_at, run_type "
        "FROM pipeline_runs ORDER BY id DESC LIMIT 1"
    ),
    "source_health": (
        "SELECT health_state, COUNT(*) AS n FROM source_health_snapshot "
        "WHERE captured_at = (SELECT MAX(captured_at) FROM source_health_snapshot) "
        "GROUP BY health_state"
    ),
    "sales_lane_counts": (
        "SELECT lane_key, COUNT(DISTINCT company_id) AS n FROM company_sales_lanes "
        "WHERE model_version = ("
        "SELECT model_version FROM company_sales_lanes "
        "ORDER BY created_at DESC LIMIT 1) "
        "GROUP BY lane_key"
    ),
    "identity_review_count": (
        "SELECT COUNT(DISTINCT company_id) AS n FROM company_sales_lanes "
        "WHERE lane_key = 'IDENTITY_REVIEW' AND model_version = ("
        "SELECT model_version FROM company_sales_lanes "
        "WHERE lane_key = 'IDENTITY_REVIEW' "
        "ORDER BY created_at DESC LIMIT 1)"
    ),
    "recorded_field_outcomes": "SELECT COUNT(*) AS n FROM sales_call_outcomes",
}
