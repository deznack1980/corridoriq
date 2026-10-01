"""The Morning Opportunity contract.

Every opportunity separates three kinds of statement:

- OBSERVED: copied from a source record (permit text, dates, status, address).
- DERIVED: a rule applied to observed fields (scope class, lane, role, recency).
- NOT_OBSERVED: things the data cannot show. They are listed so nobody reads
  them into the card. Material demand is never observed here.
"""

from __future__ import annotations

CONTRACT_VERSION = "morning-opportunity-v0.2"

# Recommended human actions. Only CALL_NOW and EMAIL are outreach actions,
# and even those are recommendations for a human; the operator sends nothing.
CALL_NOW = "CALL_NOW"
EMAIL = "EMAIL"
RESEARCH = "RESEARCH"
VERIFY_CONTRACTOR = "VERIFY_CONTRACTOR"
VERIFY_PROJECT_ROLE = "VERIFY_PROJECT_ROLE"
FOLLOW_UP = "FOLLOW_UP"
HOLD = "HOLD"
DO_NOT_CONTACT = "DO_NOT_CONTACT"

ACTIONS = (
    CALL_NOW,
    EMAIL,
    FOLLOW_UP,
    RESEARCH,
    VERIFY_CONTRACTOR,
    VERIFY_PROJECT_ROLE,
    HOLD,
    DO_NOT_CONTACT,
)
QUEUE_ACTIONS = frozenset({CALL_NOW, EMAIL, FOLLOW_UP})

# When several gates fail, the most restrictive action wins.
ACTION_SEVERITY = {
    DO_NOT_CONTACT: 0,
    HOLD: 1,
    VERIFY_CONTRACTOR: 2,
    VERIFY_PROJECT_ROLE: 3,
    RESEARCH: 4,
    FOLLOW_UP: 5,
    EMAIL: 6,
    CALL_NOW: 7,
}

HIGH = "HIGH"
MEDIUM = "MEDIUM"
LOW = "LOW"
CONFIDENCE_RANK = {HIGH: 0, MEDIUM: 1, LOW: 2}

OBSERVED = "OBSERVED"
DERIVED = "DERIVED"
NOT_OBSERVED = "NOT_OBSERVED"

# Standing disclaimers on every card.
NOT_OBSERVED_ITEMS = (
    "No BOM, RFQ, PO, or material request is observed. Material demand is not known.",
    "Buying intent and current supplier are not observed.",
    "Whether the contractor still needs material for this permit is not observed.",
)

# Outcome vocabulary for human feedback.
OUTCOMES = (
    "NO_ANSWER",
    "WRONG_CONTACT",
    "WRONG_CONTRACTOR",
    "RIGHT_CONTACT",
    "ALREADY_KNOWN",
    "NET_NEW",
    "RELEVANT",
    "NOT_RELEVANT",
    "TOO_EARLY",
    "TOO_LATE",
    "CONTACTED",
    "MEETING",
    "QUOTE",
    "FOLLOW_UP",
    "ORDER",
    "LOST",
    "BAD_DATA",
)
# A later run holds the account when the newest human outcome says the data was wrong.
NEGATIVE_DATA_OUTCOMES = frozenset({"WRONG_CONTRACTOR", "BAD_DATA", "NOT_RELEVANT"})

QUEUE_MAX = 15
REVIEW_MAX = 10
DNC_MAX = 10


def opportunity_id(company_id: int, permit_id: int) -> str:
    """Stable across runs for the same company and permit."""
    return f"mo-{int(company_id)}-{int(permit_id)}"
