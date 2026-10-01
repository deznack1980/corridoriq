"""Human outcome capture. Append-only JSONL beside the morning artifacts.

This is ground-truth collection, not a CRM. It writes nothing to the
production database, and no score reads it. The next morning run shows the
latest outcome on a card, and holds an account whose latest outcome says the
data was wrong. Nothing retrains from it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agents.ceo.morning import contract as C

NOTE_LIMIT = 500

# DESIGN ONLY. A later, separately authorized phase may move outcomes into the
# database. Do not apply this in v0.2.
PROPOSED_SQL = """
CREATE TABLE IF NOT EXISTS morning_opportunity_outcomes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id   TEXT NOT NULL,          -- mo-<company_id>-<permit_id>
    company_id       INTEGER NOT NULL REFERENCES companies(id),
    permit_id        INTEGER REFERENCES permits(id),
    project_id       INTEGER REFERENCES projects(id),
    action_taken     TEXT,                   -- CALL_NOW, EMAIL, RESEARCH, ...
    outcome          TEXT NOT NULL,          -- see OUTCOMES
    note             TEXT,
    recorded_by      TEXT,
    recorded_at      TEXT NOT NULL,
    brief_generated_at TEXT
);
""".strip()


def outcomes_path() -> Path:
    from agents.ceo.paths import output_dir

    return output_dir() / "morning" / "outcomes.jsonl"


def _find_opportunity(opportunity_id: str) -> dict | None:
    root = outcomes_path().parent
    candidates = []
    if (root / "latest_morning_queue.json").exists():
        candidates.append(root / "latest_morning_queue.json")
    candidates.extend(sorted((root / "history").glob("*_morning_queue.json"), reverse=True))
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for key in ("queue", "review", "do_not_contact"):
            for card in payload.get(key) or []:
                if card.get("opportunity_id") == opportunity_id:
                    return {"card": card, "generated_at": payload.get("generated_at")}
    return None


def record_outcome(
    opportunity_id: str,
    outcome: str,
    *,
    action: str | None = None,
    note: str | None = None,
    recorded_by: str | None = None,
) -> dict:
    from agents.ceo.security import redact

    outcome = (outcome or "").strip().upper()
    if outcome not in C.OUTCOMES:
        raise ValueError(f"Unknown outcome {outcome!r}. Use one of: {', '.join(C.OUTCOMES)}")
    if action is not None and action.upper() not in C.ACTIONS:
        raise ValueError(f"Unknown action {action!r}. Use one of: {', '.join(C.ACTIONS)}")
    found = _find_opportunity(opportunity_id)
    if found is None:
        raise LookupError(f"{opportunity_id} is not in any saved morning brief")
    card = found["card"]
    row = {
        "type": "morning_outcome",
        "opportunity_id": opportunity_id,
        "company_id": card["company"]["company_id"],
        "company": card["company"]["display_name"],
        "permit_id": card["observed"]["permit_id"],
        "permit_number": card["observed"]["permit_number"],
        "project_id": card["observed"].get("project_id"),
        "recommended_action": card.get("action"),
        "action_taken": (action or "").upper() or None,
        "outcome": outcome,
        "note": redact((note or "")[:NOTE_LIMIT]) or None,
        "recorded_by": recorded_by,
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "brief_generated_at": found["generated_at"],
    }
    path = outcomes_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row) + "\n")
    return row


def read_outcomes() -> list[dict]:
    path = outcomes_path()
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def latest_by_company() -> dict[int, dict]:
    out: dict[int, dict] = {}
    for row in read_outcomes():
        cid = row.get("company_id")
        if cid is None:
            continue
        prev = out.get(int(cid))
        if prev is None or (row.get("recorded_at") or "") >= (prev.get("recorded_at") or ""):
            out[int(cid)] = row
    return out


def schema_spec() -> dict:
    return {
        "status": "JSONL_ACTIVE_SQL_DESIGN_ONLY",
        "path": str(outcomes_path()),
        "outcomes": list(C.OUTCOMES),
        "fields": [
            "opportunity_id", "company_id", "permit_id", "project_id", "recommended_action",
            "action_taken", "outcome", "note", "recorded_by", "recorded_at", "brief_generated_at",
        ],
        "proposed_sql": PROPOSED_SQL,
        "scores_read_outcomes": False,
    }
