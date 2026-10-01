"""Future structured call-outcome model. DESIGN ONLY — not a CRM workflow."""

from __future__ import annotations

CALL_OUTCOMES = (
    "NO_ANSWER",
    "WRONG_CONTACT",
    "RIGHT_CONTACT",
    "NOT_INTERESTED",
    "CURRENT_SUPPLIER_SATISFIED",
    "OPEN_TO_SECONDARY_SUPPLIER",
    "NEEDS_QUOTE",
    "MATERIAL_LIST_REQUESTED",
    "FOLLOW_UP",
    "FULFILLMENT_INTEREST",
    "BAD_DATA",
)

LEARNING_FIELDS = (
    "verified_buyer_role",
    "actual_material_categories",
    "preferred_contact_method",
    "supplier_relationship",
    "delivery_requirements",
    "buying_frequency",
    "credit_needs",
    "service_pain_points",
    "list_format_accepted",
)

# Isolated internal schema sketch. Do not create these tables in this phase.
PROPOSED_SQL = """
-- DESIGN ONLY. Do not apply in Phase 4G.
CREATE TABLE IF NOT EXISTS sales_call_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organization_id INTEGER,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    canonical_company_id INTEGER,
    contact_channel_id INTEGER,
    user_id INTEGER,
    outcome TEXT NOT NULL,
    callability_at_time TEXT,
    notes TEXT,
    follow_up_at TEXT,
    model_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sales_call_learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outcome_id INTEGER NOT NULL REFERENCES sales_call_outcomes(id),
    field_key TEXT NOT NULL,
    field_value TEXT,
    created_at TEXT NOT NULL
);
"""


def schema_spec() -> dict:
    return {
        "status": "DESIGN_ONLY",
        "outcomes": list(CALL_OUTCOMES),
        "learning_fields": list(LEARNING_FIELDS),
        "proposed_sql": PROPOSED_SQL.strip(),
        "do_not_bulk_create_crm": True,
        "do_not_implement_fulfillment": True,
    }
