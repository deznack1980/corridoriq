"""Evidence extractors for contractor capabilities.

Phrase matching uses word boundaries so ``vegas``, ``gasket``, and
``gasoline`` do not count as fuel-gas work. Negative contexts (gas
station, etc.) suppress a generic ``gas`` hit without blocking a more
specific phrase such as ``gas line``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.contractor_intel.taxonomy import (
    SIGNAL_COMPANY_NAME,
    SIGNAL_DESCRIPTION,
    SIGNAL_LICENSE,
    SIGNAL_PERMIT_TYPE,
    SIGNAL_PROJECT_CATEGORY,
)

_WS = re.compile(r"[\s/_]+")
_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = str(value).lower().replace("-", " ")
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def _phrase_re(phrase: str, *, prefix: bool = False) -> re.Pattern[str]:
    parts = [re.escape(p) for p in phrase.split() if p]
    body = r"\s+".join(parts)
    if prefix:
        return re.compile(r"\b" + body, re.IGNORECASE)
    return re.compile(r"\b" + body + r"\b", re.IGNORECASE)


# (capability, phrase, weight, signal_type used when matched in free text)
# Longer / more specific phrases must be listed before generic "gas".
_DESCRIPTION_PHRASES: tuple[tuple[str, str, float], ...] = (
    # Fuel-gas — strong
    ("fuel_gas", "fuel gas", 24),
    ("fuel_gas", "natural gas", 22),
    ("fuel_gas", "gas line", 24),
    ("fuel_gas", "gas piping", 24),
    ("fuel_gas", "gas pipe", 24),
    ("fuel_gas", "gas service", 20),
    ("fuel_gas", "lp gas", 22),
    ("fuel_gas", "lpg", 20),
    ("fuel_gas", "propane", 22),
    ("fuel_gas", "csst", 24),
    ("fuel_gas", "tracpipe", 22),
    ("fuel_gas", "pex gas", 22),
    ("fuel_gas", "gas pex", 22),
    ("fuel_gas", "approved gas piping", 22),
    ("fuel_gas", "gas appliance connection", 24),
    ("fuel_gas", "generator gas connection", 24),
    ("fuel_gas", "gas appliance", 12),
    ("fuel_gas", "gas water heater", 24),
    ("fuel_gas", "gas furnace", 24),
    ("fuel_gas", "gas range", 6),
    ("fuel_gas", "gas dryer", 6),
    ("fuel_gas", "gas cooktop", 6),
    ("fuel_gas", "gas fireplace", 6),
    ("fuel_gas", "generator gas", 22),
    ("fuel_gas", "gas generator", 22),
    ("fuel_gas", "gas connection", 8),
    ("fuel_gas", "gas meter", 22),
    ("fuel_gas", "meter set", 16),
    ("fuel_gas", "gas distribution", 22),
    ("fuel_gas", "gas pool heater", 18),
    # Fuel-gas — lifestyle / weaker (capped later unless stronger trade evidence)
    ("fuel_gas", "outdoor kitchen", 8),
    ("fuel_gas", "bbq", 6),
    ("fuel_gas", "barbecue", 6),
    ("fuel_gas", "barbeque", 6),
    ("fuel_gas", "fire pit", 6),
    ("fuel_gas", "firepit", 6),
    ("fuel_gas", "pool heater", 8),
    # Generic gas token — recorded for audit, weight 0 so it cannot lift confidence
    ("fuel_gas", "gas", 0),
    # Plumbing
    ("plumbing", "plumbing", 22),
    ("plumbing", "plumber", 22),
    ("plumbing", "repipe", 20),
    ("plumbing", "re pipe", 20),
    ("plumbing", "water heater", 18),
    ("plumbing", "water heater replacement", 20),
    ("plumbing", "water line", 18),
    ("plumbing", "water service", 18),
    ("plumbing", "sewer", 18),
    ("plumbing", "septic", 16),
    ("plumbing", "backflow", 18),
    ("plumbing", "drain line", 16),
    ("plumbing", "drain cleaning", 14),
    ("plumbing", "fixture", 10),
    ("plumbing", "potable", 14),
    # HVAC / mechanical
    ("hvac_mechanical", "hvac", 22),
    ("hvac_mechanical", "mechanical", 18),
    ("hvac_mechanical", "air condition", 20),
    ("hvac_mechanical", "air conditioning", 20),
    ("hvac_mechanical", "heat pump", 20),
    ("hvac_mechanical", "furnace", 18),
    ("hvac_mechanical", "ductwork", 18),
    ("hvac_mechanical", "duct work", 18),
    ("hvac_mechanical", "refrigerant", 16),
    ("hvac_mechanical", "mini split", 18),
    ("hvac_mechanical", "rtu", 14),
    # Site utility
    ("site_utility", "site utility", 22),
    ("site_utility", "wet utility", 22),
    ("site_utility", "dry utility", 20),
    ("site_utility", "utility trench", 20),
    ("site_utility", "water main", 20),
    ("site_utility", "sewer main", 20),
    ("site_utility", "fire line", 16),
    ("site_utility", "underground utility", 20),
    # Electrical
    ("electrical", "electrical", 22),
    ("electrical", "electric", 16),
    ("electrical", "service panel", 18),
    ("electrical", "panel upgrade", 18),
    ("electrical", "photovoltaic", 14),
    ("electrical", "solar pv", 14),
    # Roofing / structure / finish
    ("roofing", "roofing", 22),
    ("roofing", "reroof", 20),
    ("roofing", "re roof", 20),
    ("roofing", "shingle", 14),
    ("roofing", "tile roof", 16),
    ("concrete", "concrete", 18),
    ("concrete", "flatwork", 16),
    ("framing", "framing", 20),
    ("framing", "rough frame", 18),
    ("drywall", "drywall", 22),
    ("drywall", "sheetrock", 20),
    ("flooring", "flooring", 20),
    ("flooring", "tile floor", 16),
    ("cabinetry", "cabinetry", 22),
    ("cabinetry", "cabinet", 16),
    ("cabinetry", "millwork", 16),
)

_COMPILED_DESC = tuple(
    (cap, phrase, weight, _phrase_re(phrase))
    for cap, phrase, weight in _DESCRIPTION_PHRASES
)

_NEGATIVE_GAS = _phrase_re("gas station")
_NEGATIVE_GASOLINE = _phrase_re("gasoline")
_NEGATIVE_GAS_PUMP = _phrase_re("gas pump")
_NEGATIVE_FILLING = _phrase_re("filling station")

# Permit-type / category tokens. Matched against normalized permit_type.
_TYPE_MAP: tuple[tuple[str, str, float], ...] = (
    ("plumbing", "plumbing", 25),
    ("plumbing", "plumb", 22),
    ("plumbing", "plmb", 22),
    ("fuel_gas", "fuel gas", 25),
    ("fuel_gas", "water heater gas", 22),
    ("hvac_mechanical", "mechanical", 24),
    ("hvac_mechanical", "hvac", 24),
    ("hvac_mechanical", "mech", 20),
    ("electrical", "electrical", 24),
    ("electrical", "electric", 20),
    ("electrical", "elec", 18),
    ("roofing", "roof", 20),
    ("concrete", "concrete", 20),
    ("framing", "framing", 20),
    ("drywall", "drywall", 20),
    ("flooring", "floor", 16),
    ("cabinetry", "cabinet", 18),
    ("site_utility", "utility", 16),
    ("general_contractor", "building", 12),
    ("general_contractor", "commercial", 10),
    ("general_contractor", "residential", 10),
    ("general_contractor", "bldg", 10),
)

_CATEGORY_MAP: dict[str, tuple[str, float]] = {
    "plumbing": ("plumbing", 20),
    "gas": ("fuel_gas", 20),
    "mechanical": ("hvac_mechanical", 20),
    "electrical": ("electrical", 20),
    "water heater": ("plumbing", 16),
    "roofing": ("roofing", 20),
}

_NAME_PHRASES: tuple[tuple[str, str, float], ...] = (
    ("plumbing", "plumb", 12),
    ("fuel_gas", "gas", 10),
    ("fuel_gas", "propane", 12),
    ("hvac_mechanical", "hvac", 12),
    ("hvac_mechanical", "mechanical", 12),
    ("electrical", "electric", 12),
    ("roofing", "roof", 12),
    ("concrete", "concrete", 12),
    ("framing", "fram", 10),
    ("drywall", "drywall", 12),
    ("flooring", "floor", 10),
    ("cabinetry", "cabinet", 12),
    ("site_utility", "utilit", 10),
    ("general_contractor", "construction", 12),
    ("general_contractor", "builder", 12),
    ("general_contractor", "general contract", 14),
)

_COMPILED_NAME = tuple(
    (
        cap,
        phrase,
        weight,
        _phrase_re(phrase, prefix=phrase not in {"gas"}),
    )
    for cap, phrase, weight in _NAME_PHRASES
)


@dataclass(frozen=True)
class Signal:
    capability: str
    signal_type: str
    signal_value: str
    weight: float


def _gas_negative_context(text: str) -> bool:
    return bool(
        _NEGATIVE_GAS.search(text)
        or _NEGATIVE_GASOLINE.search(text)
        or _NEGATIVE_GAS_PUMP.search(text)
        or _NEGATIVE_FILLING.search(text)
    )


def signals_from_description(description: str | None) -> list[Signal]:
    text = normalize_text(description)
    if not text:
        return []
    block_generic_gas = _gas_negative_context(text)
    out: list[Signal] = []
    seen: set[tuple[str, str]] = set()
    for cap, phrase, weight, rx in _COMPILED_DESC:
        if phrase == "gas" and block_generic_gas:
            continue
        if not rx.search(text):
            continue
        key = (cap, phrase)
        if key in seen:
            continue
        seen.add(key)
        out.append(Signal(cap, SIGNAL_DESCRIPTION, phrase, weight))
    return out


def signals_from_permit_type(permit_type: str | None) -> list[Signal]:
    text = normalize_text(permit_type)
    if not text:
        return []
    # Blanket utility ROW permits list cable/elec/gas together; that is
    # not fuel-gas contracting and is not an electrical trade permit.
    if "util permit" in text and "gas" in text:
        return [Signal("site_utility", SIGNAL_PERMIT_TYPE, "utility", 16)]
    if _gas_negative_context(text):
        # Building a gas station is not fuel-gas contracting.
        mapped = [row for row in _TYPE_MAP if row[0] != "fuel_gas"]
    else:
        mapped = list(_TYPE_MAP)
    out: list[Signal] = []
    seen: set[str] = set()
    for cap, token, weight in mapped:
        if cap in seen:
            continue
        prefix = token not in {"gas"}
        if _phrase_re(token, prefix=prefix).search(text):
            seen.add(cap)
            out.append(Signal(cap, SIGNAL_PERMIT_TYPE, token, weight))
    return out


def signals_from_category(project_category: str | None) -> list[Signal]:
    if not project_category:
        return []
    key = str(project_category).strip().lower()
    hit = _CATEGORY_MAP.get(key)
    if not hit:
        return []
    cap, weight = hit
    return [Signal(cap, SIGNAL_PROJECT_CATEGORY, key, weight)]


def signals_from_company_name(name: str | None) -> list[Signal]:
    text = normalize_text(name)
    if not text:
        return []
    if _gas_negative_context(text):
        name_map = [row for row in _COMPILED_NAME if row[0] != "fuel_gas"]
    else:
        name_map = _COMPILED_NAME
    out: list[Signal] = []
    seen: set[str] = set()
    for cap, phrase, weight, rx in name_map:
        if cap in seen:
            continue
        if rx.search(text):
            seen.add(cap)
            out.append(Signal(cap, SIGNAL_COMPANY_NAME, phrase, weight))
    return out


def signals_from_license(license_number: str | None, license_status: str | None = None) -> list[Signal]:
    """Reserved for ROC class codes. No ROC ingest in this phase.

    If a license *string* already contains an obvious trade token (rare today),
    treat it as a weak corroborating signal rather than an authoritative class.
    """
    text = normalize_text(f"{license_number or ''} {license_status or ''}")
    if not text:
        return []
    out: list[Signal] = []
    for cap, token, weight in (
        ("plumbing", "plumb", 8),
        ("fuel_gas", "gas", 8),
        ("hvac_mechanical", "mech", 8),
        ("electrical", "elec", 8),
    ):
        if _phrase_re(token, prefix=True).search(text):
            out.append(Signal(cap, SIGNAL_LICENSE, token, weight))
    return out


def extract_signals(
    *,
    permit_type: str | None,
    description: str | None,
    project_category: str | None,
    company_name: str | None,
    license_number: str | None = None,
    extra_text: str | None = None,
) -> list[Signal]:
    """All signals for one permit/project observation."""
    blob = " ".join(
        part for part in (description, extra_text) if part
    )
    out: list[Signal] = []
    out.extend(signals_from_permit_type(permit_type))
    out.extend(signals_from_description(blob))
    out.extend(signals_from_category(project_category))
    out.extend(signals_from_company_name(company_name))
    out.extend(signals_from_license(license_number))
    return out
