"""Map original job titles to decision-maker classes. Never invent titles."""

from __future__ import annotations

import re

OWNER_PRINCIPAL = "OWNER_PRINCIPAL"
PURCHASING_PROCUREMENT = "PURCHASING_PROCUREMENT"
ESTIMATING = "ESTIMATING"
PROJECT_MANAGEMENT = "PROJECT_MANAGEMENT"
OPERATIONS = "OPERATIONS"
FIELD_MANAGEMENT = "FIELD_MANAGEMENT"
OFFICE_ADMIN = "OFFICE_ADMIN"
GENERAL_CONTACT = "GENERAL_CONTACT"
UNKNOWN = "UNKNOWN"

_RULES = (
    (PURCHASING_PROCUREMENT, r"\b(purchas|procurement|buyer|supply\s*chain)\b"),
    (ESTIMATING, r"\b(estimat|bidder|bid\s*desk)"),
    (PROJECT_MANAGEMENT, r"\b(project\s*manager|\bpm\b|superintendent)\b"),
    (OPERATIONS, r"\b(operations|ops\s*manager|director\s*of\s*ops)\b"),
    (FIELD_MANAGEMENT, r"\b(foreman|field\s*manager|field\s*supervisor|crew\s*lead)\b"),
    (OWNER_PRINCIPAL, r"\b(owner|principal|president|ceo|founder|managing\s*member|proprietor)\b"),
    (OFFICE_ADMIN, r"\b(office\s*(?:manager|admin(?:istrator)?)|administrator|admin|receptionist|secretary)\b"),
    (GENERAL_CONTACT, r"\b(sales|customer\s*service|general|office|contact)\b"),
)


def classify_decision_maker(original_title: str | None) -> str:
    if not original_title or not str(original_title).strip():
        return UNKNOWN
    text = str(original_title).strip().lower()
    if re.search(r"qualifying\s*party|\bqp\b", text):
        return UNKNOWN
    for label, pattern in _RULES:
        if re.search(pattern, text):
            return label
    return UNKNOWN


def preferred_contact_type(decision_class: str, *, trade_identity: str | None = None) -> int:
    """Lower is more useful for supply-house sales. Not a rigid rank."""
    small = (trade_identity or "") in {
        "plumbing_specialist",
        "fuel_gas_specialist",
        "mechanical_wet",
        "recurring_plumbing",
    }
    order = {
        PURCHASING_PROCUREMENT: 1,
        ESTIMATING: 2,
        PROJECT_MANAGEMENT: 2,
        OPERATIONS: 3,
        OWNER_PRINCIPAL: 4 if small else 8,
        OFFICE_ADMIN: 5,
        GENERAL_CONTACT: 6,
        FIELD_MANAGEMENT: 7,
        UNKNOWN: 9,
    }
    return order.get(decision_class, 9)
