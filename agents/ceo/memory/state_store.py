"""Snapshot persistence. Previous files are not rewritten."""

from __future__ import annotations

import json
from pathlib import Path

from agents.ceo.paths import ensure_output_dir

WATCH = (
    ("sales", "actionable_public_contact_pct"),
    ("sales", "recorded_field_outcomes"),
    ("sales", "callability"),
    ("data_health", "project_count"),
    ("product", "fulfillment_implemented"),
    ("intelligence", "roc_ranking_enabled"),
)


def snapshot_dir(root: Path | None = None) -> Path:
    base = root if root is not None else ensure_output_dir()
    path = base / "snapshots"
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_snapshot(snapshot: dict, root: Path | None = None) -> str:
    directory = snapshot_dir(root)
    stamp = str(snapshot.get("generated_at", "snapshot")).replace(":", "").replace("-", "")
    name = f"{stamp}.json"
    payload = json.dumps(snapshot, indent=2, sort_keys=True, default=str)
    (directory / name).write_text(payload, encoding="utf-8")
    (directory / "latest.json").write_text(payload, encoding="utf-8")
    return name


def load_latest_snapshot(root: Path | None = None) -> dict | None:
    path = snapshot_dir(root) / "latest.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def diff_snapshots(previous: dict | None, current: dict) -> list[str]:
    if not previous:
        return ["No previous CEO snapshot. This run is the baseline."]
    changes = []
    for group, key in WATCH:
        old = (previous.get(group) or {}).get(key) or {}
        new = (current.get(group) or {}).get(key) or {}
        if old.get("status") != new.get("status") or old.get("value") != new.get("value"):
            changes.append(
                f"{group}.{key}: {old.get('status')} {old.get('value')} "
                f"to {new.get('status')} {new.get('value')}"
            )
    if not changes:
        return ["No watched operating metric changed since the previous snapshot."]
    return changes[:6]
