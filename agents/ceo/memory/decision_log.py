"""Append-only CEO decision log. Later outcomes are new lines, not edits."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from agents.ceo.paths import ensure_output_dir

OUTCOMES = ("VALIDATED", "PARTIALLY_VALIDATED", "INVALIDATED", "UNKNOWN")


def log_path(root: Path | None = None) -> Path:
    base = root if root is not None else ensure_output_dir()
    return base / "decision_log.jsonl"


def new_decision_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"ceo-{stamp}-{uuid4().hex[:8]}"


def _append(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")


def append_decision(decision, *, snapshot_ref: str, path: Path | None = None) -> dict:
    record = {
        "type": "decision",
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "decision_id": new_decision_id(),
        "snapshot_ref": snapshot_ref,
        "problem": decision.problem,
        "evidence": decision.evidence,
        "recommendation": decision.recommendation,
        "action_id": decision.action_id,
        "kind": decision.kind,
        "alternatives": decision.alternatives,
        "founder_decision": None,
        "implementation_status": "NOT_STARTED",
        "outcome": "UNKNOWN",
        "lessons": None,
        "engineering_recommended": decision.engineering,
        "execute": False,
        "confidence": decision.confidence,
        "bottleneck": decision.bottleneck,
    }
    _append(log_path() if path is None else path, record)
    return record


def record_outcome(
    decision_id: str,
    outcome: str,
    *,
    lessons: str | None = None,
    path: Path | None = None,
) -> dict:
    if outcome not in OUTCOMES:
        raise ValueError(f"Outcome must be one of {OUTCOMES}.")
    record = {
        "type": "outcome",
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "decision_id": decision_id,
        "outcome": outcome,
        "lessons": lessons,
    }
    _append(log_path() if path is None else path, record)
    return record


def read_log(path: Path | None = None) -> list[dict]:
    target = log_path() if path is None else path
    if not target.exists():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def render_log(path: Path | None = None) -> str:
    rows = read_log(path)
    if not rows:
        return "CORRIDORIQ CEO DECISIONS\nNo decisions logged yet.\n"
    lines = ["CORRIDORIQ CEO DECISIONS", ""]
    for row in rows:
        if row.get("type") == "outcome":
            lines.append(
                f"- outcome {row.get('decision_id')} {row.get('outcome')} at {row.get('timestamp')}"
            )
        else:
            lines.append(
                f"- {row.get('decision_id')} {row.get('action_id')} "
                f"confidence={row.get('confidence')} outcome={row.get('outcome')}"
            )
    return "\n".join(lines) + "\n"
