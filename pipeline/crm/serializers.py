"""Backend serializers with strict field allowlists.

Sales users only ever receive the fields listed here. Restricted data
(password hashes, secrets, DB paths, scoring weights, internal source IDs,
other orgs' CRM data) is never serialized. Filtering happens here, in the
backend — not in the UI.
"""

from __future__ import annotations

import sqlite3

# ---- Intelligence (read-only) ----------------------------------------------

# Public / intelligence company fields exposed to sales.
_COMPANY_FIELDS = (
    "id", "legal_name", "display_name", "dba_name", "company_type_primary",
    "website", "main_phone", "main_email", "address_line_1", "address_line_2",
    "city", "state", "postal_code", "country", "latitude", "longitude",
    "license_number", "license_state", "license_status", "year_established",
    "employee_range", "revenue_range", "first_seen_at", "last_seen_at",
    "lifecycle_state",
)

# Company intelligence metrics exposed to sales (derived, read-only).
_INTEL_FIELDS = (
    "total_permits", "active_permits", "total_projects", "active_projects",
    "projects_last_7_days", "projects_last_30_days", "projects_last_90_days",
    "commercial_project_count", "residential_project_count", "municipality_count",
    "first_activity_date", "latest_activity_date", "average_opportunity_score",
    "highest_opportunity_score", "estimated_opportunity_total",
    "permit_growth_30d", "permit_growth_90d", "permit_growth_12m",
    "activity_trend", "company_priority_score", "company_priority_tier",
    "metrics_calculated_at", "model_version",
)

_PROJECT_FIELDS = (
    "id", "jurisdiction", "project_category", "project_type", "project_status",
    "project_lifecycle", "opportunity_score", "opportunity_tier",
    "opportunity_date", "opportunity_timing", "recommended_action",
    "address", "city", "state",
)

_PERMIT_FIELDS = (
    "id", "jurisdiction", "permit_number", "permit_type", "status",
    "description", "issued_date", "filed_date", "city", "state", "valuation",
)

# ---- CRM (writable) --------------------------------------------------------

_RELATIONSHIP_FIELDS = (
    "id", "organization_id", "company_id", "relationship_status",
    "assigned_user_id", "assigned_by", "assigned_at", "lead_source",
    "priority_override", "do_not_contact", "do_not_contact_reason",
    "first_contact_at", "last_contact_at", "next_followup_at", "qualified_at",
    "won_at", "lost_at", "lost_reason", "created_at", "updated_at",
)

_ACTIVITY_FIELDS = (
    "id", "organization_id", "company_id", "project_id", "permit_id",
    "contact_id", "user_id", "activity_type", "activity_outcome", "subject",
    "notes", "activity_at", "duration_minutes", "next_followup_at",
    "created_at", "updated_at",
)

_TASK_FIELDS = (
    "id", "organization_id", "company_id", "project_id", "assigned_user_id",
    "created_by_user_id", "task_type", "title", "description", "priority",
    "due_at", "completed_at", "status", "created_at", "updated_at",
)

# User fields safe to expose (never password_hash / security counters).
_USER_PUBLIC_FIELDS = (
    "id", "organization_id", "email", "first_name", "last_name",
    "display_name", "phone", "is_active", "must_change_password",
    "last_login_at", "created_at",
    "account_kind", "account_state", "email_verified_at",
    "business_name", "business_category",
)


def _pick(row, keys) -> dict:
    if row is None:
        return {}
    if isinstance(row, sqlite3.Row):
        available = set(row.keys())
        return {k: row[k] for k in keys if k in available}
    return {k: row.get(k) for k in keys if k in row}


def serialize_company(row) -> dict:
    return _pick(row, _COMPANY_FIELDS)


def serialize_intelligence(row) -> dict:
    return _pick(row, _INTEL_FIELDS)


def serialize_project(row) -> dict:
    return _pick(row, _PROJECT_FIELDS)


def serialize_permit(row) -> dict:
    return _pick(row, _PERMIT_FIELDS)


def serialize_relationship(row) -> dict:
    return _pick(row, _RELATIONSHIP_FIELDS)


def serialize_activity(row) -> dict:
    return _pick(row, _ACTIVITY_FIELDS)


def serialize_task(row) -> dict:
    return _pick(row, _TASK_FIELDS)


def serialize_user(row) -> dict:
    return _pick(row, _USER_PUBLIC_FIELDS)
