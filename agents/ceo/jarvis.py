"""Future Jarvis control-plane interface.

Jarvis is not imported and not running here. These functions are the contract
a later control plane can call. They do not send mail, change DNS, or write
production data.
"""

from __future__ import annotations

from agents.ceo.approval import propose
from agents.ceo.company.loader import load_company_state
from agents.ceo.company.signals import sonoran_john

SCHEMA = "corridoriq.ceo.jarvis.v1"

INTENTS = (
    "morning_brief",
    "needs_from_archie",
    "blocking_revenue",
    "what_changed",
    "next_action",
)


def answer(intent: str, *, snapshot: dict | None = None, decision=None) -> dict:
    if intent not in INTENTS:
        raise ValueError(f"unknown Jarvis intent {intent}")
    state = load_company_state()
    body = {
        "morning_brief": _morning,
        "needs_from_archie": _needs,
        "blocking_revenue": _blocking,
        "what_changed": _changed,
        "next_action": _next,
    }[intent](state, snapshot, decision)
    return {
        "schema": SCHEMA,
        "intent": intent,
        "execute": False,
        "requires_founder_approval_to_act": True,
        "body": body,
    }


def _morning(state, snapshot, decision) -> dict:
    launch = state["launch"]
    return {
        "company": state["constitution"]["company"],
        "publicly_launched": launch["publicly_launched"],
        "landing_commit": launch["landing_commit"],
        "product_a": "supplier intelligence",
        "product_b_material_request": state["maturity"]["capabilities"][
            "contractor_material_request_text"
        ]["status"],
        "marketplace": state["maturity"]["capabilities"]["multi_supplier_marketplace"]["status"],
        "revenue": "UNKNOWN",
        "note": "Ask the CEO brief for the full morning narrative. This payload is the stable summary.",
        "snapshot_generated_at": None if not snapshot else snapshot.get("generated_at"),
        "recommendation": None if decision is None else decision.recommendation,
    }


def _needs(state, _snapshot, decision) -> dict:
    signal = sonoran_john(state)
    return {
        "decisions": [
            "Approve or decline a live demonstration of the early-access material-request flow for Sonoran. The agent will not contact John.",
            "Do not approve a public price, a contract, or a DNS cutover from this payload.",
            "Public launch stays unconfirmed until backup, checkout, runtime, Cloudflare, DNS, Microsoft 365 preservation, and a smoke test are evidenced.",
        ],
        "sonoran_recommendation": signal["RECOMMENDATION"],
        "do_now": [] if decision is None else list(decision.do_now),
    }


def _blocking(state, _snapshot, _decision) -> dict:
    return {
        "blockers": state["priorities"]["current_blockers"],
        "mrr": "UNKNOWN",
        "paid_suppliers": "UNKNOWN",
        "validated_willingness_to_pay": "UNKNOWN",
    }


def _changed(state, snapshot, decision) -> dict:
    changes = [] if decision is None else list(decision.what_changed)
    return {
        "since_previous_snapshot": changes or ["No snapshot diff was supplied."],
        "strategy_updated_at": state["constitution"]["updated_at"],
        "publicly_launched": state["launch"]["publicly_launched"],
    }


def _next(state, _snapshot, decision) -> dict:
    if decision is None:
        return {
            "recommendation": signal_next(state),
            "engineering": False,
            "proposal": propose("customer_outreach", state),
        }
    return {
        "action_id": decision.action_id,
        "recommendation": decision.recommendation,
        "bottleneck": decision.bottleneck,
        "engineering": decision.engineering,
        "execute": False,
        "proposal": propose("customer_outreach", state),
    }


def signal_next(state: dict) -> str:
    return sonoran_john(state)["RECOMMENDATION"]
