"""Multi-label wet-demand categories for plumbing-supply accounts.

PLMB permit type is not treated as fixture plumbing. A permit may carry
several labels. Internal only; does not write customer_relevance_score.
"""

from __future__ import annotations

import re

from pipeline.contractor_intel.signals import normalize_text

DEMAND_CATEGORIES = (
    "plumbing_fixture",
    "plumbing_service",
    "water_heater",
    "fuel_gas",
    "commercial_plumbing",
    "mechanical_wet",
    "civil_water",
    "civil_sewer",
    "site_utility",
    "fire_backflow",
    "pool_landscape",
    "unknown_wet_scope",
)

# Sales-relevant wet work. Pool/landscape incidental is excluded.
RELEVANT_CATEGORIES = frozenset(c for c in DEMAND_CATEGORIES if c != "pool_landscape")

# Rough buy-class weights for demand_quality_score (0-100).
CATEGORY_QUALITY = {
    "plumbing_fixture": 96.0,
    "plumbing_service": 94.0,
    "water_heater": 92.0,
    "fuel_gas": 90.0,
    "commercial_plumbing": 76.0,
    "mechanical_wet": 68.0,
    "civil_water": 58.0,
    "civil_sewer": 56.0,
    "site_utility": 54.0,
    "fire_backflow": 48.0,
    "unknown_wet_scope": 34.0,
    "pool_landscape": 16.0,
}

_WH = re.compile(r"\bwater heater|\bwtr htr\b|\bwh[- ]?replace")
_GAS = re.compile(
    r"\bfuel gas\b|\bgas line\b|\bgas pip|\bpropane\b|\bcsst\b|\blp gas\b|"
    r"\bnat(?:ural)? gas\b|\bgas meter\b|\bgas distr"
)
_FIXTURE = re.compile(
    r"\bfixture|\bgrease (?:trap|interceptor)|\brestroom|\bwater closet|"
    r"\bkitchen (?:plumb|sink)|\bpotable\b|\bbackflow preventer"
)
_SERVICE = re.compile(
    r"\brepipe\b|\bre[- ]pipe\b|\bdrain\b|\bsewer (?:line|repair|replace)\b|"
    r"\bwater (?:line|service) repair|\bleak\b|\bclog\b|\brooter\b"
)
_CIVIL_SEWER = re.compile(
    r"\b(?:onsite|on[- ]site|private)\s+sewer\b|\bsewer main\b|"
    r"\bsewer plan|\bprivate sewer\b"
)
_CIVIL_WATER = re.compile(
    r"\b(?:onsite|on[- ]site|private)\s+water\b|\bwater main\b|"
    r"\bwater services\b|\bprivate water\b|\bwaterline extension\b"
)
_SITE = re.compile(
    r"\bsite util|\bonsite util|\bon[- ]site util|\bwet util|"
    r"\bprivate water\s*(?:&|and|/)\s*sewer|\bonsite water\s*(?:&|and|/)\s*sewer"
)
_FIRE = re.compile(r"\bfire backflow\b|\bfire (?:line|sprinkler|protection)\b")
_BACKFLOW = re.compile(r"\bbackflow\b")
_POOL = re.compile(r"\bpool\b|\bspa\b|\blandscap|\birrigation\b|\bxeriscape\b")
_MECH = re.compile(r"\bmechanical\b|\bhvac\b|\bhydronic\b|\bfurnace\b|\bheat pump\b")
_PLMB_TYPE = re.compile(r"\bplumb|\bplmb\b")
_COMMERCIAL = re.compile(r"\bcommercial (?:new|miscellaneous)\b|\bcom\b")


def label_demand(
    *,
    permit_type: str | None,
    description: str | None,
    project_category: str | None,
    extra_text: str | None = None,
) -> list[str]:
    ptype = normalize_text(permit_type)
    cat = normalize_text(project_category)
    blob = normalize_text(" ".join(part for part in (description, extra_text) if part))
    text = f"{ptype} {cat} {blob}".strip()
    labels: list[str] = []

    if _WH.search(text) or cat in {"water heater"}:
        labels.append("water_heater")
    if _GAS.search(text) or cat == "gas" or "fuel gas" in ptype:
        labels.append("fuel_gas")
    if _FIXTURE.search(text):
        labels.append("plumbing_fixture")
    if _SERVICE.search(text):
        labels.append("plumbing_service")
    if _FIRE.search(text) or (_BACKFLOW.search(text) and "fire" in text):
        labels.append("fire_backflow")
    elif _BACKFLOW.search(text):
        labels.append("fire_backflow")
        if "plumbing_service" not in labels:
            labels.append("plumbing_service")
    if _CIVIL_SEWER.search(text) or (re.search(r"\bsewer\b", text) and _SITE.search(text)):
        labels.append("civil_sewer")
    if _CIVIL_WATER.search(text):
        labels.append("civil_water")
    if _SITE.search(text):
        labels.append("site_utility")
    if _POOL.search(text) and not (
        _SERVICE.search(text) or _FIXTURE.search(text) or _GAS.search(text)
    ):
        labels.append("pool_landscape")
    if _MECH.search(text) or cat == "mechanical":
        labels.append("mechanical_wet")
    if (
        _PLMB_TYPE.search(ptype) or cat in {"plumbing", "commercial"}
    ) and "confidential" in blob:
        labels.append("commercial_plumbing")
    elif (
        (_PLMB_TYPE.search(ptype) or cat == "plumbing")
        and _COMMERCIAL.search(text)
        and not any(c.startswith("civil_") or c == "site_utility" for c in labels)
        and "plumbing_fixture" not in labels
        and "plumbing_service" not in labels
        and "water_heater" not in labels
        and "fuel_gas" not in labels
    ):
        labels.append("commercial_plumbing")

    if not labels:
        if _PLMB_TYPE.search(ptype) or cat in {"plumbing", "water heater", "gas"}:
            labels.append("unknown_wet_scope")
    # De-dupe preserving order.
    seen: set[str] = set()
    out: list[str] = []
    for lab in labels:
        if lab not in seen:
            seen.add(lab)
            out.append(lab)
    return out


def primary_category(labels: list[str]) -> str | None:
    if not labels:
        return None
    return max(labels, key=lambda lab: CATEGORY_QUALITY.get(lab, 0.0))


def demand_quality(counts: dict[str, int]) -> float:
    total = sum(counts.values())
    if total <= 0:
        return 12.0
    weighted = sum(CATEGORY_QUALITY.get(k, 30.0) * n for k, n in counts.items())
    return round(weighted / total, 1)
