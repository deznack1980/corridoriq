"""Structural checks for the packaged CEO benchmarks. Not exact-text scoring."""

from __future__ import annotations

import json

from agents.ceo.briefs.cursor_brief import REQUIRED_FIELDS, missing_fields
from agents.ceo.briefs.operating_brief import render_brief
from agents.ceo.decision_engine.engine import decide
from agents.ceo.operating_state.snapshot import build_snapshot
from agents.ceo.paths import EVALS_DIR, FIXTURE_DIR
from agents.ceo.retrieval.retriever import retrieve


def evaluate_all() -> list[dict]:
    results = []
    for path in sorted((EVALS_DIR / "scenarios").glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        results.append(evaluate_scenario(scenario))
    return results


def evaluate_scenario(scenario: dict) -> dict:
    snapshot = build_snapshot(reports_dir=FIXTURE_DIR, connect_db=False)
    question = scenario["question"]
    retrieved = retrieve(question, reports_dir=FIXTURE_DIR, limit=4)
    decision = decide(snapshot, question, retrieved)
    text = render_brief(decision)
    failures = _check(decision, text, scenario.get("expect") or {})
    return {
        "id": scenario.get("id") or "scenario",
        "passed": not failures,
        "failures": failures,
        "recommendation": decision.recommendation,
        "action_id": decision.action_id,
        "bottleneck": decision.bottleneck,
        "engineering": decision.engineering,
        "confidence": decision.confidence,
        "evidence_categories": sorted(
            {
                item.get("category")
                for item in decision.evidence
                if item.get("category")
            }
        ),
    }


def format_results(results: list[dict]) -> str:
    lines = ["CORRIDORIQ CEO EVAL", ""]
    for item in results:
        mark = "PASS" if item["passed"] else "FAIL"
        lines.append(
            f"{mark} {item['id']} action={item['action_id']} "
            f"bottleneck={item['bottleneck']} engineering={item['engineering']}"
        )
        lines.append(f"  recommendation: {item['recommendation']}")
        for failure in item["failures"]:
            lines.append(f"  - {failure}")
    failed = sum(1 for item in results if not item["passed"])
    lines.append("")
    lines.append(f"{len(results) - failed} passed, {failed} failed.")
    return "\n".join(lines) + "\n"


def _check(decision, text: str, expect: dict) -> list[str]:
    failures = []
    lowered = text.lower()
    if decision.execute:
        failures.append("decision.execute must stay false")
    if len(decision.do_now) > 3:
        failures.append("DO NOW exceeds 3 items")
    if len(decision.do_not) > 3:
        failures.append("DO NOT DO YET exceeds 3 items")
    if decision.confidence not in {"HIGH", "MEDIUM", "LOW"}:
        failures.append("confidence is missing")
    if "UNKNOWN / NEEDS EVIDENCE" not in text:
        failures.append("unknown section missing")
    if "CEO CHALLENGE" not in text:
        failures.append("challenge section missing")
    if expect.get("action_id") and decision.action_id != expect["action_id"]:
        failures.append(
            f"action_id {decision.action_id} != {expect['action_id']}"
        )
    if expect.get("bottleneck") and decision.bottleneck != expect["bottleneck"]:
        failures.append(
            f"bottleneck {decision.bottleneck} != {expect['bottleneck']}"
        )
    if "engineering" in expect and decision.engineering is not expect["engineering"]:
        failures.append(
            f"engineering {decision.engineering} != {expect['engineering']}"
        )
    if expect.get("kind") and decision.kind != expect["kind"]:
        failures.append(f"kind {decision.kind} != {expect['kind']}")
    if expect.get("confidence") and decision.confidence != expect["confidence"]:
        failures.append(
            f"confidence {decision.confidence} != {expect['confidence']}"
        )
    wants_brief = expect.get("cursor_brief")
    if wants_brief is True:
        if not decision.cursor_brief:
            failures.append("cursor brief was not generated")
        else:
            missing = missing_fields(decision.cursor_brief)
            if missing:
                failures.append("cursor brief missing " + ", ".join(missing))
            blob = " ".join(decision.cursor_brief.get(field, "") for field in REQUIRED_FIELDS).lower()
            for phrase in expect.get("brief_forbids_any") or []:
                if phrase.lower() not in blob:
                    failures.append(f"cursor brief does not forbid {phrase}")
    if wants_brief is False and decision.cursor_brief:
        failures.append("cursor brief was generated when engineering was not warranted")
    phrases = expect.get("text_any") or []
    if phrases and not any(phrase.lower() in lowered for phrase in phrases):
        failures.append("brief missing any of: " + ", ".join(phrases))
    for phrase in expect.get("text_all") or []:
        if phrase.lower() not in lowered:
            failures.append(f"brief missing '{phrase}'")
    category = expect.get("evidence_category")
    if category and category not in {
        item.get("category") for item in decision.evidence
    }:
        failures.append(f"evidence missing category {category}")
    return failures
