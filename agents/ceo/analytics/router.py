"""Deterministic question routing. The model does not choose SQL."""

from __future__ import annotations

from dataclasses import dataclass, field

CITIES = {
    "phoenix": "Phoenix",
    "mesa": "Mesa",
    "tempe": "Tempe",
    "peoria": "Peoria",
    "gilbert": "Gilbert",
    "buckeye": "Buckeye",
    "scottsdale": "Scottsdale",
    "chandler": "Chandler",
    "goodyear": "Goodyear",
    "glendale": "Glendale",
    "surprise": "Surprise",
    "avondale": "Avondale",
}


@dataclass
class Plan:
    steps: list[tuple[str, dict]] = field(default_factory=list)
    status: str = "KNOWN"
    reason: str = ""


def route(question: str) -> Plan:
    text = " ".join((question or "").lower().split())
    if not text:
        return Plan(status="UNSUPPORTED", reason="empty question")
    if any(token in text for token in ("$", "dollar", "sku", "how much material", "inventory", "quote price")):
        return Plan(status="UNSUPPORTED", reason="no authoritative material, price, or inventory measure")
    if "material request" in text:
        return Plan(steps=[("get_material_requests", {})])
    if "customer book" in text or "customer-book" in text or "overlap" in text or "supplier opportunit" in text:
        return Plan(steps=[("get_customer_book_activity", {"window_days": 30})])

    steps: list[tuple[str, dict]] = []
    window = _window(text)
    city = _city(text)

    if any(token in text for token in ("since the last", "last successful", "previous refresh", "new opportunit")):
        steps.append(("get_recent_changes", {}))
    if "yesterday" in text or "since yesterday" in text or window == 1 and "change" in text:
        steps.append(("compare_activity_periods", {"window_days": 1}))
    if any(token in text for token in ("stale", "thin", "feed", "feeds", "freshness")):
        steps.append(("get_feed_freshness", {}))
    if "pipeline" in text:
        steps.append(("get_pipeline_health", {}))
    if "contact" in text:
        steps.append(("get_contact_gaps", {}))
    if "rank" in text:
        steps.append(("get_ranking_changes", {}))
    if city or any(token in text for token in ("city", "cities", "market")):
        steps.append(("get_market_activity", {"window_days": window or 30}))
        if city:
            steps.append(("get_company_activity", {"window_days": window or 30, "city": city}))
    if any(token in text for token in ("accelerat", "increase", "compare", "last 30", "30 days", "7 days", "90 days")):
        steps.append(("compare_activity_periods", {"window_days": window or 30}))
    if "multiple" in text:
        steps.append(("get_company_activity", {"window_days": 30}))
    if "why now" in text or "why-now" in text:
        steps.append(("get_top_opportunities", {}))
        steps.append(("compare_activity_periods", {"window_days": 30}))
    if "opportunit" in text and not steps:
        steps.append(("get_top_opportunities", {}))
        steps.append(("compare_activity_periods", {"window_days": 30}))
    if any(token in text for token in ("focus", "priorit", "today", "this week", "anomal")):
        focus_window = 7 if "week" in text else 30
        steps.extend([
            ("compare_activity_periods", {"window_days": focus_window}),
            ("get_contact_gaps", {}),
            ("get_feed_freshness", {}),
            ("get_pipeline_health", {}),
            ("get_top_opportunities", {}),
        ])
    if not steps and window:
        steps.append(("compare_activity_periods", {"window_days": window}))
    if not steps:
        return Plan(status="UNSUPPORTED", reason="no governed tool matches this question")
    deduped = []
    seen = set()
    for name, kwargs in steps:
        key = (name, tuple(sorted(kwargs.items())))
        if key in seen:
            continue
        seen.add(key)
        deduped.append((name, kwargs))
    return Plan(steps=deduped[:6])


def _window(text: str) -> int | None:
    if "yesterday" in text or "1 day" in text or "last day" in text:
        return 1
    if "7 day" in text or "this week" in text:
        return 7
    if "90" in text:
        return 90
    if "30" in text:
        return 30
    return None


def _city(text: str) -> str | None:
    for key, name in CITIES.items():
        if key in text:
            return name
    return None
