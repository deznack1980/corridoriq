"""Store an owner-scope morning brief after a refresh has already succeeded.

This module does not open a write connection and does not change pipeline_runs.
A generation failure leaves the previous valid brief in place.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agents.ceo.analytics.brief import DISPLAY_SECTIONS, build_daily_brief, write_brief
from agents.ceo.analytics.guard import AnalyticsError
from agents.ceo.analytics.health import apply_brief_publication, evaluate_system_health, write_health
from agents.ceo.analytics.scope import owner_scope
from agents.ceo.paths import output_dir

STATUS_NAME = "latest_brief_status.json"
SCHEMA = "corridoriq.ceo.brief_status.v1"


def intelligence_dir() -> Path:
    path = output_dir() / "intelligence"
    path.mkdir(parents=True, exist_ok=True)
    return path


def publish_owner_brief(*, db_path: Path, refresh: dict) -> dict:
    """Publish only when the caller already recorded a succeeded refresh.

    ``refresh`` is the committed run summary. This function never raises.
    """
    directory = intelligence_dir()
    health = evaluate_system_health(Path(db_path), refresh=refresh)
    attempt = {
        "refresh_run_id": refresh.get("run_id"),
        "refresh_status": refresh.get("status"),
        "refresh_completed_at": refresh.get("completed_at"),
        "attempted_at": _now(),
        "brief_status": "failed",
        "scope": {"kind": "owner", "organization_id": None},
        "error": None,
    }
    if refresh.get("status") != "succeeded" or refresh.get("run_id") is None:
        attempt["brief_status"] = "skipped"
        attempt["error"] = "refresh did not succeed"
        health = apply_brief_publication(health, status="skipped")
        write_health(health, directory)
        _write_status(directory, attempt, valid=None)
        return {"ok": False, "brief_status": "skipped", "health": health}
    try:
        completed = str(refresh.get("completed_at") or "")
        as_of = completed[:10] if len(completed) >= 10 else _now()[:10]
        brief = build_daily_brief(db_path=Path(db_path), as_of=as_of, scope=owner_scope())
        if brief.get("execute") is not False or brief.get("status") != "KNOWN" or not brief.get("display"):
            raise AnalyticsError("brief was not a known owner result")
        recent = (brief.get("sections") or {}).get("recent_changes") or {}
        brief["provenance"] = {
            "generated_at": brief.get("generated_at"),
            "data_as_of": brief.get("as_of"),
            "latest_successful_refresh": recent.get("last_successful_refresh"),
            "refresh_run_id": refresh.get("run_id"),
            "refresh_completed_at": refresh.get("completed_at"),
            "scope": {"kind": "owner", "organization_id": None},
            "generation_status": "succeeded",
            "schema": brief.get("schema"),
        }
        history_key = f"refresh-{int(refresh['run_id']):06d}"
        health = apply_brief_publication(health, status="succeeded")
        brief["health"] = health
        brief["markdown"] = (brief.get("markdown") or "") + "\nSYSTEM HEALTH\n" + health["label"] + "\n"
        paths = write_brief(brief, directory, history_key=history_key)
        paths.update(write_health(health, directory))
        attempt["brief_status"] = "succeeded"
        attempt["generated_at"] = brief.get("generated_at")
        attempt["data_as_of"] = brief.get("as_of")
        attempt["error"] = None
        valid = {
            "refresh_run_id": refresh.get("run_id"),
            "refresh_completed_at": refresh.get("completed_at"),
            "generated_at": brief.get("generated_at"),
            "data_as_of": brief.get("as_of"),
            "latest_successful_refresh": recent.get("last_successful_refresh"),
            "scope": {"kind": "owner", "organization_id": None},
            "history_key": history_key,
        }
        _write_status(directory, attempt, valid=valid)
        return {"ok": True, "brief_status": "succeeded", "paths": paths, "health": health}
    except Exception as exc:  # noqa: BLE001 - refresh status must stay succeeded
        attempt["brief_status"] = "failed"
        attempt["error"] = _safe_error(exc)
        health = apply_brief_publication(health, status="failed", error=attempt["error"])
        write_health(health, directory)
        _write_status(directory, attempt, valid=None)
        return {"ok": False, "brief_status": "failed", "error": attempt["error"], "health": health}


def read_status(directory: Path) -> dict:
    path = directory / STATUS_NAME
    empty = {"schema": SCHEMA, "latest_attempt": None, "latest_valid": None}
    if not path.is_file():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    data.setdefault("schema", SCHEMA)
    data.setdefault("latest_attempt", None)
    data.setdefault("latest_valid", None)
    return data


def _write_status(directory: Path, attempt: dict, *, valid: dict | None) -> None:
    current = read_status(directory)
    payload = {
        "schema": SCHEMA,
        "latest_attempt": attempt,
        "latest_valid": valid if valid is not None else current.get("latest_valid"),
    }
    path = directory / STATUS_NAME
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _safe_error(exc: Exception) -> str:
    if isinstance(exc, AnalyticsError):
        text = str(exc).strip()
        if text and "\\" not in text and "/" not in text and len(text) <= 160:
            return text
    return type(exc).__name__


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def required_display_ids() -> set[str]:
    return {key for key, _heading, _claim in DISPLAY_SECTIONS}
