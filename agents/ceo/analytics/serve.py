"""Owner admin view of the latest valid CEO morning brief.

The page consumes this structure. Markdown is an audit file, not the UI source.
Malformed files are not returned.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from agents.ceo.analytics.health import (
    LABELS as HEALTH_LABELS,
    evaluate_system_health,
    incomplete_health,
    load_health_record,
)
from agents.ceo.analytics.limits import REFRESH_CURRENT_HOURS
from agents.ceo.analytics.publish import read_status, required_display_ids

MISSING_MESSAGE = "No CEO Morning Brief is available yet."
MALFORMED_MESSAGE = "The latest brief file could not be validated, so it is not shown."


def load_owner_brief_view(*, reports_conn: sqlite3.Connection, directory: Path,
                          now: datetime | None = None) -> dict:
    """``now`` is injectable for tests; it defaults to the current UTC time.
    A brief or health record is current only if its refresh finished within
    REFRESH_CURRENT_HOURS of ``now``."""
    now = now or datetime.now(timezone.utc)
    latest_run = _latest_refresh(reports_conn)
    status = read_status(directory)
    brief = _valid_brief(directory / "latest_daily_brief.json")
    source = "latest"
    if brief is None and (directory / "latest_daily_brief.json").is_file():
        brief = _newest_valid_history(directory / "history")
        source = "history" if brief is not None else "malformed"
    if brief is None:
        reason = "malformed" if (directory / "latest_daily_brief.json").is_file() else "missing"
        return {
            "available": False,
            "current": False,
            "reason": reason,
            "message": MALFORMED_MESSAGE if reason == "malformed" else MISSING_MESSAGE,
            "notice": None,
            "execute": False,
            "provenance": None,
            "feeds_stale": [],
            "sections": [],
            "latest_refresh": _public_run(latest_run),
            "latest_attempt": _public_attempt(status.get("latest_attempt")),
            "health": resolve_health(directory, reports_conn, now=now),
        }
    provenance = brief["provenance"]
    current, notice = _currency(provenance, latest_run, status.get("latest_attempt"), source, now)
    return {
        "available": True,
        "current": current,
        "reason": None,
        "message": None,
        "notice": notice,
        "execute": False,
        "provenance": provenance,
        "feeds_stale": _stale_feeds(brief),
        "sections": brief["display"],
        "latest_refresh": _public_run(latest_run),
        "latest_attempt": _public_attempt(status.get("latest_attempt")),
        "health": resolve_health(directory, reports_conn, now=now),
    }


def resolve_health(directory: Path, reports_conn: sqlite3.Connection, *,
                   now: datetime | None = None) -> dict:
    """Owner health for the page. A malformed file is not treated as healthy,
    and neither is a stored record older than REFRESH_CURRENT_HOURS: health is
    only re-checked when a refresh runs, so an old "healthy" record says
    nothing about today."""
    now = now or datetime.now(timezone.utc)
    record, notice = load_health_record(directory)
    if record is None:
        live = _live_health(reports_conn)
        if live is None:
            live = incomplete_health(checked_at="", error="no_health_record")
        else:
            live = dict(live)
        if notice:
            live["artifact_notice"] = notice
            live["current"] = False
        else:
            live["current"] = True
        return live
    record = dict(record)
    age = _hours_since(record.get("checked_at"), now)
    if age is None or age > REFRESH_CURRENT_HOURS:
        when = record.get("checked_at") or "an unknown time"
        stale_notice = (f"This health check ran at {when}, more than {REFRESH_CURRENT_HOURS} hours ago. "
                        "Health is re-checked only when a refresh runs, so it may not reflect today.")
        notice = f"{notice} {stale_notice}" if notice else stale_notice
        if record.get("overall_status") == "healthy":
            # Never show an out-of-date record as healthy.
            record["recorded_overall_status"] = "healthy"
            record["overall_status"] = "unknown"
            record["label"] = HEALTH_LABELS["unknown"]
    record["artifact_notice"] = notice
    record["current"] = notice is None
    return record


def _hours_since(value, now: datetime) -> float | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds() / 3600


def _live_health(reports_conn: sqlite3.Connection) -> dict | None:
    try:
        row = reports_conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return None
    db_file = row[2] if row is not None else None
    if not db_file:
        return None
    return evaluate_system_health(Path(db_file))


def _latest_refresh(conn: sqlite3.Connection):
    try:
        return conn.execute(
            """
            SELECT id, status, completed_at
            FROM pipeline_runs
            WHERE run_type = 'morning_refresh'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return None


def _valid_brief(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != "corridoriq.ceo.daily_brief.v1":
        return None
    if data.get("execute") is not False or data.get("status") != "KNOWN":
        return None
    provenance = data.get("provenance")
    if not isinstance(provenance, dict):
        return None
    scope = provenance.get("scope") or {}
    if scope.get("kind") != "owner" or scope.get("organization_id") is not None:
        return None
    if provenance.get("generation_status") != "succeeded":
        return None
    if provenance.get("refresh_run_id") is None:
        return None
    display = data.get("display")
    if not isinstance(display, list):
        return None
    ids = [item.get("id") for item in display if isinstance(item, dict)]
    if set(ids) != required_display_ids() or any(not item.get("text") for item in display if isinstance(item, dict)):
        return None
    # Return only the fields the page is allowed to render.
    return {"provenance": _public_provenance(provenance), "display": [
        {
            "id": item["id"],
            "heading": item.get("heading"),
            "claim_class": item.get("claim_class"),
            "text": item.get("text"),
        }
        for item in display
    ], "sections": data.get("sections") if isinstance(data.get("sections"), dict) else {}}


def _newest_valid_history(history: Path) -> dict | None:
    if not history.is_dir():
        return None
    files = sorted(history.glob("refresh-*.json"), reverse=True)
    for path in files:
        brief = _valid_brief(path)
        if brief is not None:
            return brief
    return None


def _currency(provenance: dict, latest_run, attempt, source: str,
              now: datetime | None = None) -> tuple[bool, str | None]:
    brief_run = provenance.get("refresh_run_id")
    if source == "history":
        return False, (
            "The newest brief file failed validation. "
            f"Showing the last valid brief from refresh {brief_run}."
        )
    if latest_run is None:
        return False, "No morning refresh is recorded for this brief."
    latest_id = latest_run["id"]
    latest_status = latest_run["status"]
    completed = latest_run["completed_at"]
    if latest_status == "succeeded" and latest_id == brief_run:
        if isinstance(attempt, dict) and attempt.get("brief_status") == "failed" and attempt.get("refresh_run_id") == latest_id:
            return False, (
                f"Latest refresh {completed} succeeded, and brief generation failed. "
                "This page is not treating that failure as a new brief."
            )
        # Matching the latest refresh is not enough: if no refresh has run
        # since, that refresh (and this brief) can be days old.
        age = _hours_since(completed, now or datetime.now(timezone.utc))
        if age is None or age > REFRESH_CURRENT_HOURS:
            return False, (
                f"The latest refresh finished at {completed or 'an unknown time'}, more than "
                f"{REFRESH_CURRENT_HOURS} hours ago, and no newer refresh has finished. "
                "This brief may be out of date."
            )
        return True, None
    if latest_status == "running":
        return False, (
            f"A refresh is in progress. The brief below is from refresh {brief_run} "
            "and does not include it."
        )
    if latest_status != "succeeded":
        return False, (
            f"Latest refresh {completed} finished as {latest_status}. "
            f"The brief below is from refresh {brief_run} and was not generated from this refresh."
        )
    error = ""
    if isinstance(attempt, dict) and attempt.get("refresh_run_id") == latest_id and attempt.get("error"):
        error = f" Brief generation failed ({attempt.get('error')})."
    return False, (
        f"Latest refresh {completed} succeeded.{error} "
        f"Showing the last valid brief from refresh {brief_run}."
    )


def _stale_feeds(brief: dict) -> list[str]:
    freshness = (brief.get("sections") or {}).get("freshness") or {}
    rows = freshness.get("rows") if isinstance(freshness, dict) else None
    if not isinstance(rows, list):
        return []
    names = []
    for row in rows:
        if isinstance(row, dict) and row.get("stale") and row.get("jurisdiction_slug"):
            names.append(str(row["jurisdiction_slug"]))
    return names


def _public_provenance(provenance: dict) -> dict:
    return {
        "generated_at": provenance.get("generated_at"),
        "data_as_of": provenance.get("data_as_of"),
        "latest_successful_refresh": provenance.get("latest_successful_refresh"),
        "refresh_run_id": provenance.get("refresh_run_id"),
        "refresh_completed_at": provenance.get("refresh_completed_at"),
        "scope": {"kind": "owner", "organization_id": None},
        "generation_status": "succeeded",
        "schema": provenance.get("schema"),
    }


def _public_run(row) -> dict | None:
    if row is None:
        return None
    return {"id": row["id"], "status": row["status"], "completed_at": row["completed_at"]}


def _public_attempt(attempt) -> dict | None:
    if not isinstance(attempt, dict):
        return None
    return {
        "refresh_run_id": attempt.get("refresh_run_id"),
        "refresh_status": attempt.get("refresh_status"),
        "refresh_completed_at": attempt.get("refresh_completed_at"),
        "brief_status": attempt.get("brief_status"),
        "error": attempt.get("error"),
        "generated_at": attempt.get("generated_at"),
        "data_as_of": attempt.get("data_as_of"),
    }
