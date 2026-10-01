"""Date parsing for mixed source formats. Unparseable dates stay None."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

_ISO_DAY = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")


def parse_date(value) -> date | None:
    if value is None:
        return None
    match = _ISO_DAY.match(str(value).strip())
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def parse_ts(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        day = parse_date(text)
        if day is None:
            return None
        dt = datetime(day.year, day.month, day.day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def days_between(day: date | None, as_of: datetime) -> int | None:
    if day is None:
        return None
    return (as_of.date() - day).days


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# Arizona does not observe daylight saving time.
PHOENIX = timezone(timedelta(hours=-7), "America/Phoenix")


def phoenix_now() -> datetime:
    """Day windows are counted on the Phoenix calendar date."""
    return datetime.now(PHOENIX).replace(microsecond=0)


def phoenix_day(text: str) -> datetime | None:
    """YYYY-MM-DD as noon Phoenix time; anything else parsed as a timestamp."""
    day = parse_date(text)
    if day is not None and len(str(text).strip()) == 10:
        return datetime(day.year, day.month, day.day, 12, tzinfo=PHOENIX)
    return parse_ts(text)
