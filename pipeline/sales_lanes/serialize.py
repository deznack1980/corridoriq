"""Internal-only account-card serializer. Never used by the customer CRM API."""

from __future__ import annotations

INTERNAL_CARD_FIELDS = (
    "canonical_name",
    "lanes",
    "priority",
    "why_now",
    "likely_demand",
    "activity_90d",
    "historical_relevant",
    "contact_name",
    "contact_role",
    "phone",
    "email",
    "current_project",
    "identity_summary",
)

BANNED_PUBLIC_FIELDS = (
    "source_family",
    "account_priority_score",
    "customer_relevance_score",
    "match_status",
    "canonical_company_id",
    "fit",
    "model_version",
    "roc_license",
    "matching_version",
)


def serialize_internal_card(row: dict, lanes: list[str] | None = None) -> dict:
    return {
        "canonical_name": row.get("canonical_name"),
        "lanes": lanes or [],
        "priority": row.get("account_priority_score"),
        "why_now": row.get("sales_why_now"),
        "likely_demand": row.get("likely_buy"),
        "activity_90d": row.get("relevant_90d"),
        "historical_relevant": row.get("historical_relevant"),
        "contact_name": row.get("contact_name"),
        "contact_role": row.get("contact_role"),
        "phone": row.get("phone"),
        "email": row.get("email"),
        "current_project": row.get("strongest_project"),
        "identity_summary": row.get("roc_status_safe"),
    }


def serialize_internal_detail(row: dict, *, lanes: list[str], crm: dict | None) -> dict:
    card = serialize_internal_card(row, lanes)
    card.update(
        {
            "recent_projects": row.get("strongest_project"),
            "trade_identity": row.get("trade_identity"),
            "sales_lanes": lanes,
            "identity_status_safe": row.get("roc_status_safe"),
            "sales_notes": None,
            "crm_status": None if not crm else crm.get("relationship_status"),
        }
    )
    return card
