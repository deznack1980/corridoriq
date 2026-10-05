"""Render the operating brief. Internal only."""

from __future__ import annotations

from agents.ceo.briefs.executive import render_executive
from agents.ceo.decision_engine.priorities import _fmt_pct, account_names
from agents.framework.provenance import is_known


def state_lines(snapshot: dict) -> list[str]:
    sales = snapshot["sales"]
    product = snapshot["product"]
    lines: list[str] = []
    pct = sales["actionable_public_contact_pct"]
    if is_known(pct):
        lines.append(f"Frozen cohort actionable public contact: {_fmt_pct(pct)}.")
    call = sales["callability"]
    if is_known(call) and isinstance(call.get("value"), dict):
        parts = ", ".join(f"{key} {value}" for key, value in call["value"].items())
        lines.append(f"Callability on the frozen cohort: {parts}.")
    names = account_names(snapshot)
    outcomes = sales["recorded_field_outcomes"]
    if names and is_known(outcomes):
        lines.append(
            f"{len(names)} learning-trial accounts are named. "
            f"Recorded field outcomes: {outcomes.get('value')}."
        )
    fulfillment = product["fulfillment_implemented"]
    if is_known(fulfillment):
        word = "implemented" if fulfillment.get("value") else "not implemented"
        lines.append(f"Fulfillment is {word}.")
    dashboard = product["dashboard_cutover_authorized"]
    if is_known(dashboard) and dashboard.get("value") is False:
        lines.append("Customer dashboard cutover is not authorized.")
    projects = snapshot["data_health"]["project_count"]
    if is_known(projects):
        line = f"Project rows visible to this snapshot: {projects.get('value')}."
        note = projects.get("note")
        if note:
            line = f"{line} {note}"
        ignored = sales.get("ignored_newer_reports")
        if is_known(ignored):
            names = ", ".join(ignored.get("value") or [])
            line = (
                f"{line} Set aside newer sales-trial file(s) with a zero project "
                f"guardrail: {names}."
            )
        lines.append(line)
    purchasing = sales["purchasing_ops_count"]
    if is_known(purchasing) and len(lines) < 6:
        lines.append(
            "Named purchasing, estimating, or operations contacts on the frozen cohort: "
            f"{purchasing.get('value')} of {purchasing.get('n')}."
        )
    if len(lines) < 3:
        lines.append(
            "Cohort evidence is incomplete. Missing metrics stay UNKNOWN and are not estimated."
        )
    while len(lines) < 3:
        lines.append("No further verified operating fact was available.")
    return lines[:6]


def render_brief(decision) -> str:
    def bullets(items: list[str]) -> str:
        return "\n".join(f"- {item}" for item in items) or "- None."

    def numbered(items: list[str]) -> str:
        return "\n".join(f"{index}. {item}" for index, item in enumerate(items, start=1)) or "1. None."

    evidence = "\n".join(f"- {line}" for line in decision.bottleneck_evidence)
    why = "\n".join(f"- {line}" for line in decision.why)
    challenge = "\n".join(f"- {line}" for line in decision.challenge)
    unknown = "\n".join(f"- {line}" for line in decision.unknowns) or "- None material."
    changed = "\n".join(f"- {line}" for line in decision.what_changed)
    sections = [
        render_executive(decision).rstrip(),
        "",
        "DECISION DETAIL",
        "INTERNAL ONLY. Not customer-safe output. v0.2 does not execute this recommendation.",
        "",
        "CURRENT STATE",
        bullets(decision.current_state),
        "",
        "CURRENT BOTTLENECK",
        decision.bottleneck,
        "",
        "EVIDENCE",
        evidence,
        "",
        "WHY IT MATTERS",
        decision.why_it_matters,
        "",
        "WHAT WOULD REMOVE IT",
        decision.what_removes_it,
        "",
        "WHAT CHANGED",
        changed,
        "",
        "CEO RECOMMENDATION",
        f"{decision.kind}: {decision.recommendation}",
        "",
        "WHY",
        why,
        "",
        "DO NOW",
        numbered(decision.do_now),
        "",
        "DO NOT DO YET",
        numbered(decision.do_not),
        "",
        "SUCCESS CRITERIA",
        decision.success_criteria,
        "",
        "NEXT DECISION GATE",
        decision.next_gate,
        "",
        "CONFIDENCE",
        decision.confidence,
        "",
        "UNKNOWN / NEEDS EVIDENCE",
        unknown,
        "",
        "CEO CHALLENGE",
        challenge,
        "",
        f"Rule applied: {decision.rule}",
        "Engineering recommended: " + ("yes" if decision.engineering else "no"),
        "Authorized to execute: no",
    ]
    if decision.cursor_brief:
        sections.extend(["", "CURSOR BRIEF", "Generated. Founder approval is required before Cursor implements it."])
    else:
        sections.extend(
            [
                "",
                "CURSOR BRIEF",
                "Not generated. Engineering is not the recommended action.",
            ]
        )
    return "\n".join(sections) + "\n"


def render_status(snapshot: dict) -> str:
    lines = state_lines(snapshot)
    unknown_count = len(snapshot.get("unknowns") or [])
    body = "\n".join(f"- {line}" for line in lines)
    blockers = snapshot.get("known_blockers") or []
    blocker_text = "\n".join(f"- {item}" for item in blockers) or "- None derived."
    return (
        "CORRIDORIQ CEO STATUS\n"
        "INTERNAL ONLY. Read-only snapshot. No decision was logged.\n\n"
        f"Generated: {snapshot.get('generated_at')}\n\n"
        "CURRENT STATE\n"
        f"{body}\n\n"
        "KNOWN BLOCKERS\n"
        f"{blocker_text}\n\n"
        f"UNKNOWN METRICS: {unknown_count}\n"
    )
