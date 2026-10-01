"""Evidence-based WHY NOW and likely-buy summaries. No score weights."""

from __future__ import annotations

import json
import re

from pipeline.relevance.demand_taxonomy import CATEGORY_QUALITY

_BUY = {
    "fuel_gas": "gas piping / fittings / LP tanks",
    "water_heater": "water-heater materials",
    "plumbing_fixture": "fixture/service plumbing",
    "plumbing_service": "fixture/service plumbing",
    "commercial_plumbing": "commercial plumbing materials",
    "civil_water": "civil water / hydrants",
    "civil_sewer": "civil sewer",
    "site_utility": "civil water/sewer",
    "fire_backflow": "backflow assemblies / fire-protection pipe",
    "mechanical_wet": "mechanical wet-side materials",
    "pool_landscape": "landscape / pool gas (incidental)",
    "unknown_wet_scope": "unknown wet-side materials",
}

_SKIP_PRIMARY = {"unknown_wet_scope", "pool_landscape"}

OBSERVED = "OBSERVED_DEMAND"
LIKELY = "LIKELY_CATEGORY"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

_WS = re.compile(r"\s+")


def _clean(text: str | None, limit: int = 140) -> str:
    if not text:
        return ""
    t = _WS.sub(" ", str(text)).strip()
    t = t.replace("�", " ").strip()
    if len(t) > limit:
        t = t[: limit - 1].rstrip() + "…"
    return t


def demand_counts(raw) -> dict[str, int]:
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items() if v}
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return {str(k): int(v) for k, v in parsed.items() if v}
    return {}


def observed_categories(counts: dict[str, int]) -> list[str]:
    ranked = sorted(
        ((k, v) for k, v in counts.items() if k not in _SKIP_PRIMARY),
        key=lambda kv: (-kv[1], -CATEGORY_QUALITY.get(kv[0], 0)),
    )
    return [k for k, v in ranked if v > 0]


def demand_summary(counts: dict[str, int], *, trade_identity: str | None = None) -> dict:
    observed = observed_categories(counts)
    if observed and counts.get(observed[0], 0) >= 3:
        level = OBSERVED
    elif observed:
        level = LIKELY
    elif trade_identity in {"fuel_gas_specialist", "plumbing_specialist", "recurring_plumbing", "recurring_fuel_gas"}:
        level = LIKELY
        if trade_identity.startswith("fuel"):
            observed = ["fuel_gas"]
        else:
            observed = ["plumbing_service"]
    else:
        level = INSUFFICIENT
        observed = []
    buys = [_BUY.get(c, c.replace("_", " ")) for c in observed[:4]]
    return {
        "evidence_level": level,
        "observed": observed,
        "likely_buy": " • ".join(buys) if buys else "unknown",
        "primary": observed[0] if observed else None,
        "secondary": observed[1:4],
    }


def sales_why_now(
    *,
    display_name: str,
    trade_identity: str | None,
    n30: int,
    n90: int,
    n180: int,
    n_rel: int,
    n_gas: int,
    n_plum: int,
    counts: dict[str, int],
    strongest_desc: str | None,
    strongest_date: str | None,
    roc_class: str | None,
    identity_status: str | None,
) -> str:
    """Salesperson-facing explanation from actual activity. No weights."""
    demand = demand_summary(counts, trade_identity=trade_identity)
    primary = demand["primary"]
    label = (primary or "relevant").replace("_", " ")
    window = None
    if n30:
        window = f"{n30} relevant {label} permit{'s' if n30 != 1 else ''} in the last 30 days"
        if n90 and n90 != n30:
            window += f" ({n90} in 90 days)"
    elif n90:
        window = f"{n90} relevant {label} permit{'s' if n90 != 1 else ''} in the last 90 days"
    elif n180:
        window = f"{n180} relevant permits in the last 180 days"
    else:
        window = "no relevant permits in the last 90 days"

    hist = []
    if n_gas:
        hist.append(f"{n_gas} historical fuel-gas jobs")
    if n_plum:
        hist.append(f"{n_plum} historical plumbing jobs")
    if n_rel and not hist:
        hist.append(f"{n_rel} historical relevant jobs")

    text = window
    if hist:
        text = f"{window}, plus {' and '.join(hist)}"

    if roc_class:
        text += f". Active ROC class {roc_class}"
        if identity_status:
            text += f" ({identity_status.lower().replace('_', ' ')})"
    snippet = _clean(strongest_desc, 110)
    if snippet:
        when = (strongest_date or "")[:10]
        text += f". Current job{(' ' + when) if when else ''}: {snippet}"
    if not text.endswith("."):
        text += "."
    return text


def segment_for(
    *,
    trade_identity: str | None,
    counts: dict[str, int],
    roc_classes: str | None,
    curated: str | None = None,
) -> str:
    if curated:
        return curated
    classes = (roc_classes or "").upper()
    observed = observed_categories(counts)
    fireish = "C-16" in classes or "CR-16" in classes or "CR-67" in classes or "R-16" in classes
    civilish = bool(re.search(r"\bA\b|\bKA\b|\bA-12\b", classes))
    gasish = "R-37" in classes or "CR-5" in classes or "CR-80" in classes
    plumbish = "CR-37" in classes or "C-37" in classes or "R-37" in classes
    if fireish and not plumbish:
        return "FIRE_BACKFLOW"
    if civilish and not plumbish:
        return "CIVIL_WET_UTILITY"
    primary = observed[0] if observed else None
    if trade_identity in {"fuel_gas_specialist", "recurring_fuel_gas"} or primary == "fuel_gas":
        return "FUEL_GAS_PROPANE"
    if trade_identity == "gc_with_plumbing_demand":
        return "GENERAL_CONTRACTOR_CM"
    if primary in {"civil_water", "civil_sewer", "site_utility"}:
        return "CIVIL_WET_UTILITY"
    if primary == "fire_backflow":
        return "FIRE_BACKFLOW"
    if primary == "mechanical_wet":
        return "HVAC_MECHANICAL"
    if trade_identity in {"plumbing_specialist", "recurring_plumbing"}:
        return "RESIDENTIAL_PLUMBING"
    return "UNKNOWN"
