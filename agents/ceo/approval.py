"""Founder approval gates. Proposing an action never performs it."""

from __future__ import annotations

from agents.ceo.company.loader import load_company_state


def gates(state: dict | None = None) -> dict:
    return (state or load_company_state())["approval_gates"]


def requires_approval(action: str, state: dict | None = None) -> bool:
    listed = set(gates(state)["requires_founder_approval"])
    allowed = set(gates(state)["allowed_without_approval"])
    if action in allowed and action not in listed:
        return False
    if action in listed:
        return True
    return True


def propose(action: str, state: dict | None = None) -> dict:
    """A proposal the founder can accept later. execute is always false."""
    return {
        "action": action,
        "requires_founder_approval": requires_approval(action, state),
        "execute": False,
        "performed": False,
    }


def execute(_action: str, *_args, **_kwargs):
    raise PermissionError(
        "CEO agent v0.2 cannot execute gated or ungated external actions."
    )
