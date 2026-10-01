"""Evidence items attached to a decision. Categories stay distinct."""

from __future__ import annotations

from agents.framework.provenance import (
    FIELD_EVIDENCE,
    INTERNAL_HYPOTHESIS,
    MODEL_INFERENCE,
    is_known,
)


def from_metric(label: str, item: dict) -> dict | None:
    if not is_known(item):
        return {
            "statement": f"{label} is UNKNOWN. {item.get('note') or ''}".strip(),
            "source": item.get("source"),
            "timestamp": item.get("timestamp"),
            "category": None,
            "confidence": None,
        }
    return {
        "statement": f"{label}: {item.get('value')}",
        "source": item.get("source"),
        "timestamp": item.get("timestamp"),
        "category": item.get("category"),
        "confidence": item.get("confidence"),
    }


def founder_claim(question: str) -> dict:
    return {
        "statement": question.strip(),
        "source": "founder_question",
        "timestamp": None,
        "category": FIELD_EVIDENCE,
        "confidence": "MEDIUM",
        "provenance": (
            "Founder statement in the CEO question. "
            "It is not yet a row in the call-outcome log."
        ),
    }


def hypothesis(statement: str, source: str) -> dict:
    return {
        "statement": statement,
        "source": source,
        "timestamp": None,
        "category": INTERNAL_HYPOTHESIS,
        "confidence": "MEDIUM",
    }


def inference(statement: str) -> dict:
    return {
        "statement": statement,
        "source": "agents.ceo.decision_engine",
        "timestamp": None,
        "category": MODEL_INFERENCE,
        "confidence": "MEDIUM",
    }


def compact(items: list[dict | None]) -> list[dict]:
    return [item for item in items if item]
