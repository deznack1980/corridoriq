"""Recency and source-freshness labels for project feeds (opportunities, map,
dashboard activity lists). Pure functions; nothing here writes.

One rule set for every feed:

- A project's activity date is its source issued date, else its source filed
  date, else its stored opportunity date (itself a source date). Ingestion
  timestamps are never used as activity dates, and undated or future-dated
  rows are not "recent".
- The recent window is gates.REVIEW_WINDOW_DAYS (60); inside it, rows older
  than gates.CALL_WINDOW_DAYS (30) are labelled as outside the call window.
- Source freshness comes from trust.health (the same reading that gates
  Today's accounts). Rows from a STALE or NO_DATA feed are labelled.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from pipeline.trust.dates import parse_date
from pipeline.trust.gates import CALL_WINDOW_DAYS, REVIEW_WINDOW_DAYS
from pipeline.trust.health import LAGGING, NO_DATA, STALE

# SQL for the activity date of a projects pr JOIN permits p row. The same
# expression bounds every "recent" feed.
ACTIVITY_DATE_SQL = (
    "substr(COALESCE(NULLIF(TRIM(p.issued_date),''), p.filed_date, pr.opportunity_date),1,10)"
)

RECENT = "RECENT"            # within the call window
OUTSIDE_CALL_WINDOW = "OUTSIDE_CALL_WINDOW"  # inside the review window only
HISTORICAL = "HISTORICAL"    # older than the review window
UNDATED = "UNDATED"          # no usable source date (or a future date)

_LABELS = {
    RECENT: "Recent activity",
    OUTSIDE_CALL_WINDOW: f"Older than {CALL_WINDOW_DAYS} days",
    HISTORICAL: f"History (older than {REVIEW_WINDOW_DAYS} days)",
    UNDATED: "No usable source date",
}


def utc_today(now: datetime | None = None) -> date:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date()


def window_bounds(now: datetime | None = None) -> tuple[str, str]:
    """(start, end) ISO days of the recent window, inclusive."""
    today = utc_today(now)
    return (today - timedelta(days=REVIEW_WINDOW_DAYS)).isoformat(), today.isoformat()


def activity_of(row: dict) -> tuple[str | None, str | None]:
    """(activity day, basis) from source fields only."""
    for field, basis in (("issued_date", "issued"), ("filed_date", "filed"),
                         ("opportunity_date", "opportunity")):
        day = parse_date(row.get(field))
        if day is not None:
            return day.isoformat(), basis
    return None, None


def recency_state(days: int | None) -> str:
    if days is None or days < 0:
        return UNDATED
    if days <= CALL_WINDOW_DAYS:
        return RECENT
    if days <= REVIEW_WINDOW_DAYS:
        return OUTSIDE_CALL_WINDOW
    return HISTORICAL


def source_index(health: dict | None) -> dict[str, dict]:
    return {s["jurisdiction"]: s for s in (health or {}).get("sources") or []}


def describe(row: dict, sources: dict[str, dict], today: date) -> dict:
    """Recency + source-freshness fields for one row. Never raises on bad data."""
    day, basis = activity_of(row)
    days = None if day is None else (today - date.fromisoformat(day)).days
    state = recency_state(days)
    src = sources.get(row.get("jurisdiction")) if sources else None
    freshness = (src or {}).get("freshness") or NO_DATA
    source_stale = freshness in {STALE, NO_DATA} or bool((src or {}).get("call_blocked"))
    notes = []
    if state == UNDATED:
        notes.append("No usable source date; not treated as current.")
    elif state == HISTORICAL:
        notes.append(f"Last source activity {days} days ago; history, not a current opportunity.")
    elif state == OUTSIDE_CALL_WINDOW:
        notes.append(f"Last source activity {days} days ago; outside the {CALL_WINDOW_DAYS}-day call window.")
    if source_stale:
        newest = (src or {}).get("newest_record_date") or "unknown"
        notes.append(f"This city's permit feed is not current (newest record {newest}); verify before outreach.")
    elif freshness == LAGGING:
        notes.append("This city's permit feed is more than a week behind.")
    return {
        "activity_date": day,
        "activity_basis": basis,
        "days_since_activity": days if state != UNDATED else None,
        "recency": state,
        "recency_label": _LABELS[state],
        "source_freshness": freshness,
        "source_stale": source_stale,
        "freshness_note": " ".join(notes) or None,
    }


def attach(items: list[dict], health: dict | None, today: date) -> None:
    """Add recency fields to each row in place."""
    sources = source_index(health)
    for it in items:
        it.update(describe(it, sources, today))

