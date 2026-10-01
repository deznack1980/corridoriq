"""Permit-level trade demand for a plumbing-supply customer.

This is about the *job*, not the company. A GC restaurant build with
plumbing in scope can still have demand. A forklift dealer's racking
permit does not.
"""

from __future__ import annotations

import re

from pipeline.contractor_intel.signals import normalize_text
from pipeline.contractor_intel.taxonomy import (
    CORE_PLUMBING_PHRASES,
    GAS_PIPING_PHRASES,
    STRONG_GAS_PHRASES,
)

_PLUMB_TYPE = re.compile(r"\bplumb|\bplmb\b|\bwater heater")
_GAS_TYPE = re.compile(r"\bfuel gas\b|\bwater heater gas\b")
_MECH_TYPE = re.compile(r"\bmechanical\b|\bhvac\b")
_UTIL_TYPE = re.compile(r"\butility\b|\bsite util")
_RACKING = re.compile(
    r"\brack(?:ing|s)?\b|\bhigh[\s-]*pil(?:e|ed)\b|\bforklift\b|"
    r"\bpallet rack\b|\bmaterial handling\b|\btoyotalift\b|"
    r"\bstorage rack\b|\bcombustible storage\b"
)
_PLUMB_EXTRA = (
    "water heater", "repipe", "re pipe", "sewer", "septic", "backflow",
    "potable", "drain line", "plumbing", "plumber", "water line",
    "water service", "fixture",
)
_MECH_EXTRA = ("hvac", "mechanical", "furnace", "air condition", "heat pump", "ductwork")
_SITE_EXTRA = ("site utility", "wet utility", "water main", "sewer main", "fire line")


def demand_score(
    *,
    permit_type: str | None,
    description: str | None,
    project_category: str | None,
    extra_text: str | None = None,
) -> tuple[float, list[str]]:
    """Return (0-100 demand, flag list) for plumbing_supply."""
    ptype = normalize_text(permit_type)
    blob = normalize_text(" ".join(part for part in (description, extra_text) if part))
    cat = (project_category or "").strip()
    flags: list[str] = []
    score = 12.0

    if _RACKING.search(ptype) or _RACKING.search(blob):
        flags.append("racking_or_material_handling")
        # A racking job can still mention fire sprinklers; that is not
        # plumbing-supply demand on its own.
        if not (_PLUMB_TYPE.search(ptype) or any(p in blob for p in CORE_PLUMBING_PHRASES)):
            return 6.0, flags

    if _PLUMB_TYPE.search(ptype) or cat in {"Plumbing", "Water Heater"}:
        score = max(score, 90.0)
        flags.append("plumbing_permit_or_category")
    if _GAS_TYPE.search(ptype) or cat == "Gas":
        score = max(score, 82.0)
        flags.append("fuel_gas_permit_or_category")
    if any(p in blob for p in CORE_PLUMBING_PHRASES) or any(p in blob for p in _PLUMB_EXTRA):
        score = max(score, 84.0)
        flags.append("plumbing_scope")
    if any(p in blob for p in GAS_PIPING_PHRASES) or any(p in blob for p in STRONG_GAS_PHRASES):
        score = max(score, 80.0)
        flags.append("fuel_gas_scope")
    if _MECH_TYPE.search(ptype) or cat == "Mechanical" or any(p in blob for p in _MECH_EXTRA):
        score = max(score, 58.0)
        flags.append("mechanical_scope")
    if _UTIL_TYPE.search(ptype) or cat == "Industrial" or any(p in blob for p in _SITE_EXTRA):
        score = max(score, 52.0)
        flags.append("site_utility_scope")
    if cat in {"Restaurant", "Medical", "Hotel", "Apartment"} and score < 50:
        score = max(score, 42.0)
        flags.append("wet_sector_possible_demand")
    if cat in {"Warehouse", "Manufacturing"} and score < 40 and "racking_or_material_handling" not in flags:
        score = max(score, 28.0)
        flags.append("industrial_shell")

    return round(min(100.0, score), 1), flags
