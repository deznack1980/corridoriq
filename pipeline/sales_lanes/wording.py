"""Customer-safe identity wording. No source-family codes or match internals."""

from __future__ import annotations

from pipeline.sales_lanes.assign import PLUMBING_LICENSE, GAS_PIPING_LICENSE, FIRE_LICENSE, CIVIL_LICENSE
from pipeline.sales_lanes.lanes import (
    CIVIL_WET_UTILITY,
    FIRE_BACKFLOW,
    FUEL_GAS_PROPANE,
    GENERAL_CONTRACTOR_CM,
    PLUMBING_CORE,
)


def identity_safe(identity_status: str | None, classes: list[str], lanes: list[str]) -> str:
    status = (identity_status or "").upper()
    cls = {c.upper().replace(" ", "") for c in classes}
    if set(cls) & PLUMBING_LICENSE:
        base = "Licensed plumbing contractor"
    elif set(cls) & GAS_PIPING_LICENSE:
        base = "Licensed gas-piping contractor"
    elif set(cls) & FIRE_LICENSE:
        base = "Licensed fire-protection contractor"
    elif set(cls) & CIVIL_LICENSE:
        base = "Licensed civil / underground contractor"
    elif PLUMBING_CORE in lanes:
        base = "Plumbing contractor"
    elif FUEL_GAS_PROPANE in lanes:
        base = "Fuel-gas / propane contractor"
    elif FIRE_BACKFLOW in lanes:
        base = "Fire / backflow specialist"
    elif CIVIL_WET_UTILITY in lanes:
        base = "Civil wet-utility contractor"
    elif GENERAL_CONTRACTOR_CM in lanes:
        base = "General contractor / construction manager"
    else:
        base = "Contractor"
    if status in {"VERIFIED", "VERIFIED_MATCH"}:
        return f"{base} (identity confirmed)"
    if status in {"HIGH_CONFIDENCE", "HIGH_CONFIDENCE_MATCH"}:
        return f"{base} (identity high confidence)"
    if status in {"POSSIBLE", "POSSIBLE_MATCH"}:
        return f"{base} (confirm legal entity on first call)"
    if status in {"CONFLICT", "CONFLICT_MATCH"}:
        return f"{base} (identity conflict — confirm before quoting)"
    return base
