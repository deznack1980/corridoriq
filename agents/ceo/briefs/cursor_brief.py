"""Cursor execution brief. Produced only when engineering is recommended.

The brief is an instruction. v0.1 does not run it.
"""

from __future__ import annotations

REQUIRED_FIELDS = (
    "TITLE",
    "BUSINESS OBJECTIVE",
    "WHY NOW",
    "CURRENT VERIFIED STATE",
    "SCOPE",
    "AUTHORIZED CHANGES",
    "NOT AUTHORIZED",
    "FILES / COMPONENTS LIKELY INVOLVED",
    "IMPLEMENTATION REQUIREMENTS",
    "ACCEPTANCE CRITERIA",
    "TEST REQUIREMENTS",
    "GUARDRAILS",
    "RETURN FORMAT",
    "STOP CONDITION",
)


def build_cursor_brief(decision, snapshot: dict) -> dict | None:
    if not decision.engineering:
        return None
    if decision.action_id != "constrained_intake_prototype":
        return None
    outcomes = snapshot["sales"]["recorded_field_outcomes"]
    outcome_note = (
        f"Snapshot recorded_field_outcomes={outcomes.get('value')} ({outcomes.get('status')})."
    )
    return {
        "TITLE": "Constrained photo supply-list intake prototype",
        "BUSINESS OBJECTIVE": (
            "Learn whether CorridorIQ can capture a real contractor supply list sent as a "
            "texted photo of a handwritten list, without building a fulfillment marketplace."
        ),
        "WHY NOW": (
            "Founder-reported field evidence says contractors from the learning trial want "
            "to text photos of handwritten lists. Partner routing, pricing, and order "
            "workflow are not justified by that evidence."
        ),
        "CURRENT VERIFIED STATE": (
            "CEO v0.1 is read-only and has not implemented intake. "
            f"{outcome_note} Confirm the photo claim in the call notes before merging any code. "
            "Scores, sales lanes, CRM, ROC ranking, and the customer dashboard stay as they are."
        ),
        "SCOPE": (
            "An internal prototype that accepts an image, stores it outside production ranking "
            "tables, and shows the image to an internal user. No partner routing. No customer login."
        ),
        "AUTHORIZED CHANGES": (
            "Add an isolated prototype path and tests for image accept, store, and internal view. "
            "Keep files out of score calculation, lane assignment, and CRM writes."
        ),
        "NOT AUTHORIZED": (
            "Full fulfillment marketplace. Partner selection. Quote or order workflow. "
            "Email or SMS sending. Changes to opportunity_score, customer_relevance_score, "
            "account_priority_score, sales-lane rows, CRM rows, company merges, project foreign keys, "
            "ROC ranking enablement, dashboard cutover, deployment, or git push."
        ),
        "FILES / COMPONENTS LIKELY INVOLVED": (
            "A new isolated prototype module chosen with the founder. Do not modify "
            "pipeline/analysis, pipeline/relevance score writers, pipeline/sales_lanes assignment, "
            "pipeline/crm service writes, or the customer dashboard ranking path."
        ),
        "IMPLEMENTATION REQUIREMENTS": (
            "Accept a photo and a free-text note. Persist the file in a non-production location. "
            "Record the sending account name and timestamp. Do not parse the photo into an order. "
            "Do not call suppliers. Failure to read the handwriting is a valid outcome and must be visible."
        ),
        "ACCEPTANCE CRITERIA": decision.success_criteria,
        "TEST REQUIREMENTS": (
            "Unit tests for accept, reject of a non-image, and internal retrieval. "
            "A guardrail test that score tables and CRM row counts are unchanged. "
            "Run the existing CorridorIQ suite and do not merge if it regresses."
        ),
        "GUARDRAILS": (
            "Read the founder's approval before editing. Stop on any need to write production "
            "scores or contact a customer automatically. The CEO agent will not run this brief."
        ),
        "RETURN FORMAT": (
            "Return files changed, tests run, a statement that scores, lanes, CRM, and production "
            "data were not modified, and any real photo that was or was not readable."
        ),
        "STOP CONDITION": (
            "Stop if the work would alter ranking scores, sales-lane storage, CRM, ROC enablement, "
            "the customer dashboard, or if fewer than the agreed accounts will actually send a photo."
        ),
    }


def render_cursor_brief(brief: dict) -> str:
    lines = [
        "CORRIDORIQ CURSOR BRIEF",
        "INTERNAL. Founder approval required. Do not execute from the CEO agent.",
        "",
    ]
    for field in REQUIRED_FIELDS:
        lines.append(field)
        lines.append(brief.get(field) or "UNKNOWN")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def missing_fields(brief: dict) -> list[str]:
    return [field for field in REQUIRED_FIELDS if not str(brief.get(field) or "").strip()]
