"""Assign companies to one or more sales lanes. Does not write scores."""

from __future__ import annotations

import re

from pipeline.roc.normalize import looks_like_person_name
from pipeline.sales_gate.evidence import demand_counts, observed_categories
from pipeline.sales_lanes.lanes import (
    BOOK_FITS,
    CIVIL_WET_UTILITY,
    EXCLUDED,
    FIRE_BACKFLOW,
    FUEL_GAS_PROPANE,
    GAS_PIPING_CONTRACTOR,
    GENERAL_CONTRACTOR_CM,
    HIGH,
    HVAC_MECHANICAL,
    IDENTITY_REVIEW,
    LOW,
    MEDIUM,
    MIXED_PLUMBING_GAS,
    MULTI_TRADE,
    PLUMBING_CORE,
    PROPANE_RETAIL_EXCHANGE,
    PROPANE_SERVICE,
    PROPANE_TANK_INSTALL,
    SPECIALTY_OTHER,
)

_PLUMB = re.compile(r"\bplumb(?:ing|er)?\b", re.I)
_PROPANE = re.compile(r"\bpropane\b|\blp\s*gas\b|\bamerigas\b|\bferrell\b", re.I)
_GAS_PIPE = re.compile(r"\bgas piping\b|\bgas services?\b|\bgas pipe\b", re.I)
_FIRE = re.compile(r"\bfire\b|backflow|\bsprinkler\b|\bmetering\b", re.I)
_EXPEDITE = re.compile(r"\bpermit service\b|\bpermit runner\b|\bexpedit", re.I)
_LIFT = re.compile(r"toyotalift|\bforklift\b|\bpallet\b|\bmaterial handling\b", re.I)
_LANDSCAPE = re.compile(r"\blandscap|\benvironments\b|\bhardscape|\birrigat", re.I)
_POOL = re.compile(r"\bpool\b|\bspa\b|\bjacuzzi\b", re.I)
_GC = re.compile(
    r"\bcommercial\b|\bhomes?\b|\bbuilders?\b|\bintegrated services\b|\bconstruction manager\b",
    re.I,
)
_MUNI = re.compile(r"\bcity of\b|\btown of\b|\bwater (?:dept|department|services)\b", re.I)
_CAGE = re.compile(r"exchange cage|exchange program|lpg exchange", re.I)

PLUMBING_LICENSE = {"CR-37", "C-37", "R-37", "C-77", "CR-77", "R-77"}
GAS_PIPING_LICENSE = {"R-37R", "C-37R"}
FIRE_LICENSE = {"C-16", "CR-16", "R-16", "CR-67", "C-67", "R-67"}
CIVIL_LICENSE = {"A", "KA", "A-12", "A-16", "CR-80", "C-80"}
GC_LICENSE = {"B", "B-1", "B-2", "B-3", "KB-1", "KB-2"}
HVAC_LICENSE = {"CR-39", "C-39", "R-39", "CR-58", "C-58", "CR-4", "C-4"}

PLUMBING_DEMAND = ("plumbing_fixture", "plumbing_service", "water_heater", "commercial_plumbing")
CIVIL_DEMAND = ("civil_water", "civil_sewer", "site_utility")


def _sum(counts: dict[str, int], keys) -> int:
    return sum(int(counts.get(k) or 0) for k in keys)


def _classes(raw) -> set[str]:
    if not raw:
        return set()
    if isinstance(raw, (list, tuple, set)):
        return {str(x).upper().replace(" ", "") for x in raw if x}
    return {p.strip().upper().replace(" ", "") for p in str(raw).replace(",", ";").split(";") if p.strip()}


def classify_features(feat: dict) -> list[dict]:
    """Pure assignment. feat is a dict of name/identity/demand/ROC/person fields."""
    name = feat.get("display_name") or ""
    identity = feat.get("trade_identity") or ""
    counts = demand_counts(feat.get("demand_mix") or feat.get("demand_categories") or {})
    classes = _classes(feat.get("roc_classes"))
    person = feat.get("person_class") or "not_person_name"
    n_plum = int(feat.get("n_plum") or 0)
    n_gas = int(feat.get("n_gas") or 0)
    blob = feat.get("permit_blob") or ""

    plum_d = _sum(counts, PLUMBING_DEMAND)
    gas_d = int(counts.get("fuel_gas") or 0) or n_gas
    fire_d = int(counts.get("fire_backflow") or 0)
    civil_d = _sum(counts, CIVIL_DEMAND)
    mech_d = int(counts.get("mechanical_wet") or 0)
    observed = observed_categories(counts)

    has_plumb_lic = bool(classes & PLUMBING_LICENSE)
    has_gas_pipe_lic = bool(classes & GAS_PIPING_LICENSE)
    has_fire_lic = bool(classes & FIRE_LICENSE)
    has_civil_lic = bool(classes & CIVIL_LICENSE)
    has_gc_lic = bool(classes & GC_LICENSE)
    has_hvac_lic = bool(classes & HVAC_LICENSE)

    plumber_name = bool(_PLUMB.search(name))
    propane_name = bool(_PROPANE.search(name))
    gas_pipe_name = bool(_GAS_PIPE.search(name))
    fire_name = bool(_FIRE.search(name))
    landscape_name = bool(_LANDSCAPE.search(name))
    pool_name = bool(_POOL.search(name))
    lift_name = bool(_LIFT.search(name))
    muni_name = bool(_MUNI.search(name))
    expedite_name = bool(_EXPEDITE.search(name))
    person_like = looks_like_person_name(name)

    not_ready = bool(
        expedite_name
        or person in {
            "permit_applicant",
            "owner_builder",
            "unresolved_individual",
            "qualifying_party_as_company",
            "entity_resolution_artifact",
        }
        or (person_like and person not in {"licensed_sole_proprietor", "company_name_false_positive", "not_person_name"})
    )
    presentable = 0 if not_ready else 1

    rows: list[dict] = []

    def add(lane, fit, *, subtype=None, evidence=()):
        rows.append(
            {
                "lane_key": lane,
                "fit": fit,
                "subtype": subtype,
                "evidence": list(evidence),
                "presentable": presentable if lane != IDENTITY_REVIEW else 0,
            }
        )

    if not_ready:
        why = []
        if expedite_name:
            why.append("permit_expeditor")
        if person_like or person != "not_person_name":
            why.append(person or "person_name")
        add(IDENTITY_REVIEW, HIGH, evidence=why)
        return rows

    if muni_name or lift_name:
        add(
            SPECIALTY_OTHER,
            HIGH,
            evidence=["municipality"] if muni_name else ["equipment_dealer"],
        )
        # Still allow other lanes below for municipalities? No — exclude sales motions.
        if lift_name or muni_name:
            return rows

    # --- fire ---
    fire_primary = has_fire_lic and not plumber_name and not has_plumb_lic
    fire_house = fire_name and not plumber_name
    if fire_name or fire_primary or (fire_d >= 8 and fire_d >= plum_d and not plumber_name):
        fit = HIGH if (fire_name or fire_primary or fire_d >= 10) else MEDIUM
        add(FIRE_BACKFLOW, fit, evidence=["fire_license"] if has_fire_lic else ["fire_demand"])
    elif fire_d >= 3 and (plumber_name or has_plumb_lic):
        add(FIRE_BACKFLOW, MEDIUM, evidence=["plumber_backflow_activity"])

    # --- civil ---
    civil_primary = has_civil_lic and not plumber_name and not has_plumb_lic and not gas_pipe_name
    if civil_primary or (civil_d >= 8 and civil_d > plum_d and not plumber_name and identity != "plumbing_specialist"):
        add(
            CIVIL_WET_UTILITY,
            HIGH if has_civil_lic or civil_d >= 10 else MEDIUM,
            evidence=["civil_license"] if has_civil_lic else ["civil_demand"],
        )

    # --- GC / CM ---
    gc_primary = identity == "gc_with_plumbing_demand" or (
        has_gc_lic and not plumber_name and not propane_name and not gas_pipe_name and not fire_name
    )
    if gc_primary and not plumber_name and not fire_name and not fire_primary:
        add(GENERAL_CONTRACTOR_CM, HIGH, evidence=["gc_identity"] if identity == "gc_with_plumbing_demand" else ["gc_license"])

    # --- fuel gas ---
    fuel_specialist = identity in {"fuel_gas_specialist", "recurring_fuel_gas"}
    landscape_only = landscape_name and not plumber_name and not propane_name and not gas_pipe_name
    propane_company = propane_name or (
        fuel_specialist and plum_d < 3 and n_plum < 3 and int(counts.get("water_heater") or 0) < 5
        and not plumber_name
    )
    cage = bool(_CAGE.search(blob)) or (
        propane_name and "AMERIGAS" in name.upper()
    )
    fuel_fit = None
    fuel_sub = None
    if landscape_only:
        fuel_fit = LOW
        fuel_sub = None
    elif propane_name or gas_pipe_name or fuel_specialist or has_gas_pipe_lic or gas_d >= 8:
        if cage or "AMERIGAS" in name.upper():
            fuel_fit = MEDIUM
            fuel_sub = PROPANE_RETAIL_EXCHANGE
        elif "FERRELL" in name.upper():
            fuel_fit = MEDIUM
            fuel_sub = PROPANE_SERVICE
        elif propane_name:
            fuel_fit = HIGH
            fuel_sub = PROPANE_TANK_INSTALL if gas_d >= 8 else PROPANE_SERVICE
        elif gas_pipe_name or has_gas_pipe_lic:
            fuel_fit = HIGH
            fuel_sub = GAS_PIPING_CONTRACTOR
        elif fuel_specialist:
            fuel_fit = HIGH
            fuel_sub = PROPANE_TANK_INSTALL if gas_d >= 8 else PROPANE_SERVICE
        else:
            fuel_fit = MEDIUM
            fuel_sub = PROPANE_SERVICE
        if (
            (plumber_name or plum_d >= 5)
            and gas_d >= 3
            and not propane_name
            and not propane_company
        ):
            fuel_sub = MIXED_PLUMBING_GAS
            fuel_fit = HIGH
    if fuel_fit and fuel_fit in BOOK_FITS:
        add(FUEL_GAS_PROPANE, fuel_fit, subtype=fuel_sub, evidence=["fuel_gas_activity"])
    elif fuel_fit == LOW:
        add(FUEL_GAS_PROPANE, LOW, evidence=["landscape_incidental_gas"])

    # --- HVAC ---
    if has_hvac_lic or mech_d >= 20:
        add(
            HVAC_MECHANICAL,
            HIGH if has_hvac_lic or mech_d >= 50 else MEDIUM,
            evidence=["hvac_license"] if has_hvac_lic else ["mechanical_wet_demand"],
        )

    # --- specialty ---
    if landscape_only or (pool_name and not plumber_name):
        add(SPECIALTY_OTHER, HIGH, evidence=["landscape_or_pool"])

    # --- plumbing core (strict) ---
    fire_dominant = (
        not plumber_name
        and int(counts.get("water_heater") or 0) < 5
        and (
            fire_house
            or fire_primary
            or (fire_d >= 8 and fire_d >= plum_d)
        )
    )
    exclude_core = (
        fire_primary
        or fire_house
        or fire_dominant
        or civil_primary
        or (gc_primary and not plumber_name and not fire_name)
        or landscape_only
        or propane_company
        or lift_name
        or muni_name
    )
    include_core = (
        plumber_name
        or (has_plumb_lic and (identity in {"plumbing_specialist", "recurring_plumbing"} or plum_d >= 3 or n_plum >= 3))
        or (identity in {"plumbing_specialist", "recurring_plumbing"} and plum_d >= 5)
        or int(counts.get("water_heater") or 0) >= 20
        or (has_plumb_lic and has_hvac_lic and (plum_d >= 3 or n_plum >= 3))
    )
    if include_core and not exclude_core:
        fit = HIGH if plumber_name or (has_plumb_lic and plum_d >= 3) or int(counts.get("water_heater") or 0) >= 20 else MEDIUM
        add(PLUMBING_CORE, fit, evidence=["plumbing_contractor"])
        if gas_d >= 5 or fuel_specialist:
            # mixed plumber+gas already added fuel lane; subtype mixed if plumber
            pass
    elif has_plumb_lic and fire_d >= 3 and plumber_name:
        add(PLUMBING_CORE, MEDIUM, evidence=["plumber_current_backflow"])

    lanes = {r["lane_key"] for r in rows if r["fit"] in BOOK_FITS}
    core_adjacent = {PLUMBING_CORE, FUEL_GAS_PROPANE, HVAC_MECHANICAL, FIRE_BACKFLOW, CIVIL_WET_UTILITY}
    if len(lanes & core_adjacent) >= 2:
        add(MULTI_TRADE, HIGH, evidence=["multi_lane_account"])

    if not any(r["lane_key"] != IDENTITY_REVIEW and r["fit"] in BOOK_FITS for r in rows):
        add(SPECIALTY_OTHER, MEDIUM, evidence=["unclassified_presentable"])

    return rows


def presentable_in_lane(assignments: list[dict], lane_key: str) -> bool:
    return any(
        r["lane_key"] == lane_key and r["fit"] in BOOK_FITS and int(r.get("presentable") or 0)
        for r in assignments
    )
