"""Evidence categories. These are not interchangeable."""

from __future__ import annotations

VERIFIED_SYSTEM_STATE = "VERIFIED_SYSTEM_STATE"
FIELD_EVIDENCE = "FIELD_EVIDENCE"
EXTERNAL_EVIDENCE = "EXTERNAL_EVIDENCE"
INTERNAL_HYPOTHESIS = "INTERNAL_HYPOTHESIS"
MODEL_INFERENCE = "MODEL_INFERENCE"

CATEGORIES = (
    VERIFIED_SYSTEM_STATE,
    FIELD_EVIDENCE,
    EXTERNAL_EVIDENCE,
    INTERNAL_HYPOTHESIS,
    MODEL_INFERENCE,
)

KNOWN = "KNOWN"
UNKNOWN = "UNKNOWN"


def metric(
    value,
    *,
    source: str,
    category: str,
    timestamp: str | None = None,
    confidence: str = "HIGH",
    note: str | None = None,
    **extra,
) -> dict:
    if category not in CATEGORIES:
        raise ValueError(f"Unknown evidence category: {category}")
    item = {
        "status": KNOWN,
        "value": value,
        "source": source,
        "category": category,
        "timestamp": timestamp,
        "confidence": confidence,
        "note": note,
    }
    item.update(extra)
    return item


def unknown(note: str, *, source: str | None = None) -> dict:
    return {
        "status": UNKNOWN,
        "value": None,
        "source": source,
        "category": None,
        "timestamp": None,
        "confidence": None,
        "note": note,
    }


def is_known(item) -> bool:
    return isinstance(item, dict) and item.get("status") == KNOWN


def value_of(item, default=None):
    if is_known(item):
        return item.get("value", default)
    return default
