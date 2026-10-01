"""Official Arizona ROC classification → CorridorIQ capability map.

Meanings are taken from https://roc.az.gov/license-classifications.
Unlisted codes stay unknown. Do not guess.
"""

from __future__ import annotations

from pipeline.config.settings import ROC_CLASS_MAP_VERSION, ROC_CLASSIFICATIONS_URL
from pipeline.roc.normalize import normalize_class_code

CAP_PLUMBING = "plumbing"
CAP_FUEL_GAS = "fuel_gas"
CAP_HVAC = "hvac_mechanical"
CAP_SITE = "site_utility"
CAP_GC = "general_contractor"
CAP_ELECTRICAL = "electrical"
CAP_ROOFING = "roofing"
CAP_CONCRETE = "concrete"
CAP_FRAMING = "framing"
CAP_DRYWALL = "drywall"
CAP_FLOORING = "flooring"
CAP_CABINETRY = "cabinetry"
CAP_FIRE = "fire_protection"
CAP_POOL = "pool"
CAP_LANDSCAPE = "landscape"
CAP_UNKNOWN = "unknown"

# C / R / CR specialty triples share one official scope family.
_TRIPLES = {
    "1": (CAP_UNKNOWN, "Acoustical Systems", (), 1.0),
    "3": (CAP_UNKNOWN, "Awnings, Canopies, Carports and Patio Covers", (), 1.0),
    "4": (CAP_HVAC, "Boilers, Steamfitting and Process Piping", (CAP_FUEL_GAS,), 1.0),
    "6": (CAP_POOL, "Swimming Pool Service and Repair", (), 1.0),
    "7": (CAP_FRAMING, "Carpentry", (), 1.0),
    "8": (CAP_FLOORING, "Floor Covering", (), 1.0),
    "9": (CAP_CONCRETE, "Concrete", (), 1.0),
    "10": (CAP_DRYWALL, "Drywall", (), 1.0),
    "11": (CAP_ELECTRICAL, "Electrical", (), 1.0),
    "12": (CAP_UNKNOWN, "Elevators", (), 1.0),
    "14": (CAP_UNKNOWN, "Fencing", (), 1.0),
    "15": (CAP_UNKNOWN, "Blasting", (), 1.0),
    "16": (CAP_FIRE, "Fire Protection Systems", (), 1.0),
    "21": (CAP_LANDSCAPE, "Hardscaping and Irrigation Systems", (), 1.0),
    "24": (CAP_UNKNOWN, "Ornamental Metals", (), 1.0),
    "31": (CAP_UNKNOWN, "Masonry", (), 1.0),
    "34": (CAP_UNKNOWN, "Painting and Wall Covering", (), 1.0),
    "36": (CAP_UNKNOWN, "Plastering", (), 1.0),
    "37": (CAP_PLUMBING, "Plumbing", (CAP_FUEL_GAS,), 1.0),
    "39": (CAP_HVAC, "Air Conditioning and Refrigeration", (CAP_FUEL_GAS,), 0.9),
    "40": (CAP_UNKNOWN, "Insulation", (), 1.0),
    "41": (CAP_SITE, "Septic Tanks and Systems", (), 1.0),
    "42": (CAP_ROOFING, "Roofing", (), 1.0),
    "45": (CAP_UNKNOWN, "Sheet Metal", (), 1.0),
    "48": (CAP_UNKNOWN, "Ceramic, Plastic and Metal Tile", (), 1.0),
    "53": (CAP_SITE, "Water Well Drilling / Drilling", (), 1.0),
    "54": (CAP_PLUMBING, "Water Conditioning Equipment", (), 0.9),
    "56": (CAP_UNKNOWN, "Welding", (), 1.0),
    "57": (CAP_UNKNOWN, "Wrecking", (), 1.0),
    "60": (CAP_CABINETRY, "Finish Carpentry", (), 1.0),
    "61": (CAP_GC, "Carpentry, Remodeling and Repairs", (), 0.75),
    "65": (CAP_UNKNOWN, "Glazing", (), 1.0),
    "67": (CAP_ELECTRICAL, "Low Voltage Communication Systems", (), 0.8),
    "70": (CAP_UNKNOWN, "Reinforcing Bar and Wire Mesh", (), 1.0),
}

_EXPLICIT: dict[str, tuple] = {
    "A": (CAP_SITE, "General Engineering", (), 0.7),
    "A-4": (CAP_SITE, "Drilling", (), 1.0),
    "A-5": (CAP_SITE, "Excavating, Grading and Oil Surfacing", (), 0.8),
    "A-7": (CAP_CONCRETE, "Piers and Foundations", (), 1.0),
    "A-9": (CAP_POOL, "Swimming Pools", (CAP_FUEL_GAS,), 0.7),
    "A-11": (CAP_UNKNOWN, "Steel and Aluminum Erection", (), 1.0),
    "A-12": (CAP_SITE, "Sewers, Drains and Pipe Laying", (), 1.0),
    "A-14": (CAP_UNKNOWN, "Asphalt Paving", (), 1.0),
    "A-15": (CAP_UNKNOWN, "Seal Coating", (), 1.0),
    "A-16": (CAP_SITE, "Waterworks", (), 1.0),
    "A-17": (CAP_ELECTRICAL, "Electrical and Transmission Lines", (), 1.0),
    "A-19": (CAP_POOL, "Swimming Pools, Including Solar", (CAP_FUEL_GAS,), 0.7),
    "B": (CAP_GC, "General Residential Contractor", (), 1.0),
    "B-1": (CAP_GC, "General Commercial Contractor", (), 1.0),
    "B-2": (CAP_GC, "General Small Commercial Contractor", (), 1.0),
    "B-3": (CAP_GC, "General Remodeling and Repair Contractor", (), 1.0),
    "B-4": (CAP_GC, "General Residential Engineering Contractor", (), 0.85),
    "B-5": (CAP_POOL, "General Swimming Pool Contractor", (), 1.0),
    "B-6": (CAP_POOL, "General Swimming Pool Contractor, Including Solar", (), 1.0),
    "B-10": (CAP_POOL, "Pre-Manufactured Spas and Hot Tubs", (), 1.0),
    "KA": (CAP_SITE, "Dual Engineering", (), 0.7),
    "KA-5": (CAP_POOL, "Dual Swimming Pool Contractor", (), 1.0),
    "KA-6": (CAP_POOL, "Dual Swimming Pool Contractor, Including Solar", (), 1.0),
    "KB-1": (CAP_GC, "Dual Building Contractor", (), 1.0),
    "KB-2": (CAP_GC, "Dual Residential and Small Commercial", (), 1.0),
    "CR-2": (CAP_SITE, "Excavating, Grading and Oil Surfacing", (), 0.8),
    "R-2": (CAP_SITE, "Excavating, Grading and Oil Surfacing", (), 0.8),
    "CR-17": (CAP_UNKNOWN, "Steel and Aluminum Erection", (), 1.0),
    "R-17": (CAP_UNKNOWN, "Structural Steel and Aluminum", (), 1.0),
    "CR-58": (CAP_HVAC, "Comfort Heating, Ventilating, Evaporative Cooling", (CAP_FUEL_GAS,), 0.9),
    "C-58": (CAP_HVAC, "Comfort Heating, Ventilating, Evaporative Cooling", (CAP_FUEL_GAS,), 0.9),
    "CR-66": (CAP_UNKNOWN, "Seal Coating", (), 1.0),
    "CR-69": (CAP_UNKNOWN, "Asphalt Paving", (), 1.0),
    "R-13": (CAP_UNKNOWN, "Asphalt Paving", (), 1.0),
    "CR-74": (CAP_HVAC, "Boilers, Steamfitting and Process Piping, Including Solar", (CAP_FUEL_GAS,), 1.0),
    "C-74": (CAP_HVAC, "Boilers, Steamfitting and Process Piping, Including Solar", (CAP_FUEL_GAS,), 1.0),
    "C-77": (CAP_PLUMBING, "Plumbing Including Solar", (CAP_FUEL_GAS,), 1.0),
    "CR-77": (CAP_PLUMBING, "Plumbing Including Solar", (CAP_FUEL_GAS,), 1.0),
    "R-37": (CAP_PLUMBING, "Plumbing, Including Solar", (CAP_FUEL_GAS, CAP_FIRE), 1.0),
    "C-78": (CAP_PLUMBING, "Solar Plumbing, Liquid Systems Only", (), 0.85),
    "CR-78": (CAP_PLUMBING, "Solar Plumbing, Liquid Systems Only", (), 0.85),
    "C-79": (CAP_HVAC, "Air Conditioning and Refrigeration, Including Solar", (CAP_FUEL_GAS,), 0.9),
    "CR-79": (CAP_HVAC, "Air Conditioning and Refrigeration, Including Solar", (CAP_FUEL_GAS,), 0.9),
    "CR-80": (CAP_SITE, "Sewers, Drains and Pipe Laying", (), 1.0),
    "R-62": (CAP_GC, "Minor Home Improvements", (), 0.7),
    "C-62": (CAP_GC, "Minor Home Improvements", (), 0.7),
}

_DETAIL_OVERRIDES = (
    ("GAS PIPING", CAP_FUEL_GAS, (CAP_PLUMBING,), "R-37R Gas Piping", 1.0),
    ("SEWERS, DRAINS AND PIPE LAYING", CAP_SITE, (), "Sewers, Drains and Pipe Laying", 1.0),
    ("SEWERS DRAINS AND PIPE LAYING", CAP_SITE, (), "Sewers, Drains and Pipe Laying", 1.0),
    ("SWIMMING POOL PLUMBING", CAP_POOL, (CAP_PLUMBING,), "Swimming Pool Plumbing and Equipment", 0.9),
    ("SOLAR PLUMBING", CAP_PLUMBING, (), "Solar Plumbing Liquid Systems Only", 0.85),
    ("CENTRAL VACUUM", CAP_UNKNOWN, (), "Built-in Central Vacuum Systems", 1.0),
    ("FIXTURE REFINISHING", CAP_PLUMBING, (), "Kitchen and Bathroom Fixture Refinishing", 0.8),
)


def _triple_entries() -> dict[str, tuple]:
    out = dict(_EXPLICIT)
    for num, payload in _TRIPLES.items():
        for prefix in ("C", "R", "CR"):
            code = f"{prefix}-{num}"
            out.setdefault(code, payload)
        out.setdefault(f"R-{num}R", payload)
        out.setdefault(f"C-{num}R", payload)
    return out


CLASS_MAP = _triple_entries()


def map_classification(raw_class: str | None, class_detail: str | None = None) -> dict:
    code = normalize_class_code(raw_class, class_detail)
    detail = (class_detail or "").upper()
    for needle, cap, secondary, title, conf in _DETAIL_OVERRIDES:
        if needle in detail:
            return _pack(code or raw_class or "", cap, secondary, title, conf, "detail_override")
    entry = CLASS_MAP.get(code)
    if entry is None:
        return _pack(code or (raw_class or "").strip().upper(), CAP_UNKNOWN, (), None, 0.0, "unmapped")
    cap, title, secondary, conf = entry
    return _pack(code, cap, secondary, title, conf, "official_scope")


def _pack(code, cap, secondary, title, conf, notes) -> dict:
    return {
        "normalized_class": code,
        "official_title": title,
        "corridor_capability": cap,
        "secondary_capabilities": list(secondary),
        "mapping_confidence": conf,
        "mapping_version": ROC_CLASS_MAP_VERSION,
        "source_url": ROC_CLASSIFICATIONS_URL,
        "notes": notes,
    }


def seed_classification_rows() -> list[tuple]:
    rows = []
    seen = set()
    for code, entry in sorted(CLASS_MAP.items()):
        if code in seen:
            continue
        seen.add(code)
        cap, title, secondary, conf = entry
        packed = _pack(code, cap, secondary, title, conf, "official_scope")
        rows.append(
            (
                packed["normalized_class"],
                packed["official_title"],
                packed["corridor_capability"],
                ",".join(packed["secondary_capabilities"]),
                packed["mapping_confidence"],
                packed["mapping_version"],
                packed["source_url"],
                packed["notes"],
            )
        )
    return rows


def capabilities_for_licenses(mapped_rows: list[dict]) -> list[str]:
    caps: list[str] = []
    seen = set()
    for row in mapped_rows:
        for cap in [row.get("corridor_capability"), *(row.get("secondary_capabilities") or [])]:
            if cap and cap != CAP_UNKNOWN and cap not in seen:
                seen.add(cap)
                caps.append(cap)
    return caps
