"""Bounds for governed analytics. These are limits, not score weights."""

from __future__ import annotations

MAX_ROWS = 25
MAX_TOOL_CALLS = 6
ALLOWED_WINDOWS = (1, 7, 30, 90)
MAX_WINDOW_DAYS = 90
STALE_SOURCE_DAYS = 45
HIGH_PRIORITY_SCORE = 70
PLUMBING_PROFILE = "plumbing_supply"
PLUMBING_IDENTITIES = ("plumbing_specialist", "recurring_plumbing")
USABLE_CONTACT_TYPES = ("business_phone", "business_email")
PROGRESS_STEPS = 200_000
