"""Customer-safe views of trust-layer output for the sales portal.

Allow-list only: every field is built here on purpose. No internal score, lane
fit, match status, license wording, source family, gate name, or contact value
is returned. Uncertainty is translated to plain language, not dropped.
"""

from __future__ import annotations

from pipeline.trust import contract as C
from pipeline.trust import gates as G
from pipeline.trust.account_view import LANE_LABELS

ACTION_TEXT = {C.CALL_NOW: "Call", C.EMAIL: "Email", C.FOLLOW_UP: "Follow up"}
CONFIDENCE_TEXT = {C.HIGH: "High", C.MEDIUM: "Medium", C.LOW: "Low"}

# Flags a rep needs to see, in words. Flags not listed are internal-only.
FLAG_TEXT = {
    "ROLE_FROM_ACCOUNT_HISTORY": "The permit does not name who does the work; this company's past permits suggest it does.",
    "ROLE_CONFIDENCE_BELOW_75": "Evidence that this company does this kind of work is moderate.",
    "DUPLICATE_NAME_PEER_UNMERGED": "Another company record has a similar name. Confirm you have the right business.",
    "SOURCE_LAGGING": "This city's permit feed is more than a week behind.",
    "REFRESH_AGING": "The data was last refreshed more than 36 hours ago.",
}
IDENTITY_TEXT = {G.VERIFIED: "Business identity confirmed", G.HIGH_CONFIDENCE: "Business identity high confidence"}
CONTACT_TEXT = {
    G.VERIFIED_PHONE: "Verified business phone on file",
    G.VERIFIED_EMAIL: "Verified business email on file",
}


def public_card(card: dict) -> dict:
    obs, der = card["observed"], card["derived"]
    flags = card["gate_result"]["flags"]
    return {
        "opportunity_id": card["opportunity_id"],
        "company_id": card["company"]["company_id"],
        "display_name": card["company"]["display_name"],
        "action": ACTION_TEXT.get(card["action"], "Review"),
        "confidence": CONFIDENCE_TEXT.get(card["confidence"], "Low"),
        "why_now": card["why_now"],
        "trade": der["scope_label"],
        "lane": LANE_LABELS.get(der["lane"], der["lane"]),
        "trade_evidence": sorted({t for terms in der["matched_terms"].values() for t in terms})[:6],
        "project": {
            "project_id": obs.get("project_id"),
            "permit_number": obs.get("permit_number"),
            "jurisdiction": obs.get("jurisdiction"),
            "address": obs.get("address"),
            "city": obs.get("city"),
            "activity_date": obs.get("activity_date"),
            "permit_status": obs.get("permit_status"),
            "description": obs.get("description"),
        },
        "identity": IDENTITY_TEXT.get(card["company"]["identity"]["status"], "Business identity not confirmed"),
        "contact": CONTACT_TEXT.get(card["contact"]["state"], "No verified contact on file"),
        "verify_before_contact": list(card["human_verification_required"]),
        "uncertainty": [FLAG_TEXT[f] for f in flags if f in FLAG_TEXT],
        "not_observed": list(card["not_observed"]),
    }


def public_data_status(health: dict) -> dict:
    refresh = health.get("refresh") or {}
    state = refresh.get("state") or "UNKNOWN"
    return {
        "refresh": state.title(),
        "hours_since_refresh": refresh.get("hours_since_last_success"),
        "stale_sources": [s.replace("_az", "").replace("_", " ").title() for s in health.get("stale_sources") or []],
        "note": (
            "Recommendations are withheld until the data refreshes."
            if state in {"STALE", "UNKNOWN"}
            else None
        ),
    }


def public_relevance(rel: dict | None) -> dict:
    if not rel:
        return {"status": "NOT_ASSESSED", "label": "Not yet assessed", "lanes": []}
    return {"status": rel["status"], "label": rel["label"], "lanes": list(rel["lanes"])}
