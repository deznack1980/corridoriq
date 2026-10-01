"""Matching keys for supplier accounts and shared-intelligence records.

Reuses CorridorIQ's existing normalizers read-only (company names, phones, ROC
licenses). Address normalization lives here because no shared one exists; it
is deliberately simple and only ever used together with a ZIP or city.
Source values are never modified — these functions return separate keys.
"""

from __future__ import annotations

import re

from pipeline.company_resolution.normalize import normalize_city, normalize_phone
from pipeline.entity.names import GENERIC_TOKENS, compact_company_name, is_person, name_tokens
from pipeline.roc.normalize import normalize_roc_license

_ZIP5 = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_UNIT = re.compile(r"\s(?:STE|SUITE|UNIT|APT|BLDG|BUILDING|SPC|SPACE|LOT|RM|ROOM|FL|FLOOR)\b.*$|\s#.*$")
_STREET_WORDS = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD", "DRIVE": "DR",
    "BOULEVARD": "BLVD", "LANE": "LN", "PARKWAY": "PKWY", "PLACE": "PL", "COURT": "CT",
    "HIGHWAY": "HWY", "CIRCLE": "CIR", "TRAIL": "TRL", "WAY": "WAY", "TERRACE": "TER",
    "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}


def name_key(raw) -> str:
    """Comparison key for a company name (suffixes, punctuation, THE removed)."""
    return compact_company_name(raw) if raw else ""


def distinctive(raw) -> set[str]:
    return {t for t in name_tokens(raw) if t not in GENERIC_TOKENS and len(t) > 1}


def name_similarity(a, b) -> float:
    """Token overlap of two compacted names (0..1). Never decisive alone."""
    sa, sb = name_tokens(a), name_tokens(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def person_name(raw) -> bool:
    return bool(raw) and is_person(compact_company_name(raw))


def phone_key(raw) -> str | None:
    return normalize_phone(raw)


def license_key(raw) -> str | None:
    return normalize_roc_license(raw)


def city_key(raw) -> str | None:
    return normalize_city(raw)


def zip5(raw) -> str | None:
    if not raw:
        return None
    m = _ZIP5.search(str(raw))
    return m.group(1) if m else None


def street_key(raw) -> str | None:
    """'1201 West Main Street, Ste 4' → '1201 W MAIN ST'. Needs a house number."""
    if not raw:
        return None
    s = str(raw).upper().split(",")[0]
    s = re.sub(r"[.’']", "", s)
    s = re.sub(r"[^A-Z0-9# ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _UNIT.sub("", " " + s).strip()
    tokens = [_STREET_WORDS.get(t, t) for t in s.split()]
    if len(tokens) < 2 or not tokens[0].isdigit():
        return None
    return " ".join(tokens)


def address_parts(street=None, city=None, postal=None, full=None) -> dict:
    """Street key + ZIP + city from either separate fields or one line
    ('123 W Main St, Phoenix, AZ 85001')."""
    if full and not street:
        parts = [p.strip() for p in str(full).split(",")]
        street = parts[0] if parts else None
        if city is None and len(parts) >= 3:
            city = parts[1]
        postal = postal or zip5(full)
    return {"street": street_key(street), "zip5": zip5(postal), "city": city_key(city)}
