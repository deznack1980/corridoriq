"""Inclusive calendar windows. Dates are YYYY-MM-DD."""

from __future__ import annotations

from datetime import date, timedelta

from agents.ceo.analytics.guard import AnalyticsError
from agents.ceo.analytics.limits import ALLOWED_WINDOWS


def parse_day(value: str) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise AnalyticsError("unreadable date") from exc


def require_window(days: int) -> int:
    if int(days) not in ALLOWED_WINDOWS:
        raise AnalyticsError(f"window must be one of {ALLOWED_WINDOWS}")
    return int(days)


def bounds(as_of: str, days: int) -> tuple[str, str]:
    days = require_window(days)
    end = date.fromisoformat(parse_day(as_of))
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def prior_bounds(as_of: str, days: int) -> tuple[str, str]:
    days = require_window(days)
    end = date.fromisoformat(parse_day(as_of)) - timedelta(days=days)
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()
