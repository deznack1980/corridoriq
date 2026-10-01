"""Plumbing-core quality labels. Flags mistakes; does not retune ranking."""

from __future__ import annotations

import re

CLEARLY_BELONGS = "CLEARLY_BELONGS"
PROBABLY_BELONGS = "PROBABLY_BELONGS"
AMBIGUOUS = "AMBIGUOUS"
PROBABLY_WRONG = "PROBABLY_WRONG_LANE"
CLEARLY_WRONG = "CLEARLY_WRONG_LANE"

_PLUMB = re.compile(r"\bplumb", re.I)
_FIRE = re.compile(r"\bfire\b|\bbackflow\b", re.I)
_CIVIL = re.compile(r"\bcivil\b|\bsewer\b|\bhydrant\b", re.I)
_GC = re.compile(r"\bcommercial\b|\bintegrated\b", re.I)
_PROPANE = re.compile(r"\bpropane\b|\bamerigas\b|\bferrell\b", re.I)


def label_plumbing_core(rec: dict, roc_classes: list[str] | None = None) -> str:
    name = rec.get("canonical_name") or ""
    ident = rec.get("trade_identity") or ""
    demand = rec.get("primary_demand") or ""
    n_plum_hist = int(rec.get("historical_relevant") or 0)
    classes = " ".join(roc_classes or [])
    if _FIRE.search(name) or (demand == "fire_backflow" and not _PLUMB.search(name)):
        if "CR-37" not in classes and "C-37" not in classes and not _PLUMB.search(name):
            return CLEARLY_WRONG
        return PROBABLY_WRONG
    if _PROPANE.search(name) and not _PLUMB.search(name):
        return PROBABLY_WRONG
    if ident == "gc_with_plumbing_demand" or _GC.search(name):
        return PROBABLY_WRONG
    if demand in {"civil_water", "civil_sewer", "site_utility"} and not _PLUMB.search(name):
        return PROBABLY_WRONG
    if _PLUMB.search(name) and ident in {"plumbing_specialist", "recurring_plumbing"}:
        return CLEARLY_BELONGS
    if "CR-37" in classes or "C-37" in classes or "R-37" in classes:
        if demand in {"plumbing_fixture", "plumbing_service", "water_heater", "commercial_plumbing", "fuel_gas"}:
            return CLEARLY_BELONGS if _PLUMB.search(name) else PROBABLY_BELONGS
        return PROBABLY_BELONGS
    if ident in {"plumbing_specialist", "recurring_plumbing"} and demand in {
        "plumbing_fixture", "plumbing_service", "water_heater", "commercial_plumbing", "fuel_gas",
    }:
        return PROBABLY_BELONGS
    if n_plum_hist >= 50:
        return PROBABLY_BELONGS
    return AMBIGUOUS
