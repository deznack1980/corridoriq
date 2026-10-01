"""Turn a snapshot and a question into one recommendation.

The action is chosen in ``priorities``. This module attaches the challenge,
the state lines, and the Cursor brief. It never executes the brief.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from agents.ceo.briefs.cursor_brief import build_cursor_brief
from agents.ceo.briefs.operating_brief import state_lines
from agents.ceo.decision_engine.challenge import challenge
from agents.ceo.decision_engine.priorities import classify_proposal, select, trial_ready
from agents.ceo.memory.state_store import diff_snapshots


@dataclass
class Decision:
    action_id: str
    kind: str
    recommendation: str
    bottleneck: str
    bottleneck_evidence: list[str]
    why_it_matters: str
    what_removes_it: str
    why: list[str]
    do_now: list[str]
    do_not: list[str]
    success_criteria: str
    next_gate: str
    confidence: str
    unknowns: list[str]
    challenge: list[str]
    evidence: list[dict]
    alternatives: list[dict]
    engineering: bool
    problem: str
    rule: str
    current_state: list[str]
    what_changed: list[str]
    cursor_brief: dict | None = None
    execute: bool = False
    extras: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.execute = False
        self.do_now = list(self.do_now)[:3]
        self.do_not = list(self.do_not)[:3]
        self.current_state = list(self.current_state)[:6]


def decide(
    snapshot: dict,
    question: str,
    retrieved: list[dict] | None = None,
    previous: dict | None = None,
) -> Decision:
    selected = select(snapshot, question)
    proposal = selected.get("challenge_proposal") or classify_proposal(question)
    lines = challenge(
        proposal,
        trial_ready=trial_ready(snapshot),
        engineering=bool(selected["engineering"]),
    )
    decision = Decision(
        action_id=selected["action_id"],
        kind=selected["kind"],
        recommendation=selected["recommendation"],
        bottleneck=selected["bottleneck"],
        bottleneck_evidence=list(selected["bottleneck_evidence"]),
        why_it_matters=selected["why_it_matters"],
        what_removes_it=selected["what_removes_it"],
        why=list(selected["why"]),
        do_now=list(selected["do_now"]),
        do_not=list(selected["do_not"]),
        success_criteria=selected["success_criteria"],
        next_gate=selected["next_gate"],
        confidence=selected["confidence"],
        unknowns=list(selected["unknowns"]),
        challenge=lines,
        evidence=list(selected["evidence"]),
        alternatives=list(selected["alternatives"]),
        engineering=bool(selected["engineering"]),
        problem=question.strip() or "What should CorridorIQ do next?",
        rule=selected["rule"],
        current_state=state_lines(snapshot),
        what_changed=diff_snapshots(previous, snapshot),
    )
    if retrieved:
        decision.extras["retrieved_sources"] = [
            {
                "source": item.get("source"),
                "category": item.get("category"),
                "timestamp": item.get("timestamp"),
                "confidence": item.get("confidence"),
            }
            for item in retrieved[:4]
        ]
    if decision.engineering:
        decision.cursor_brief = build_cursor_brief(decision, snapshot)
    return decision
