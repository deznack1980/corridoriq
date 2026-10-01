"""Shadow normalization. Original strings are never modified; these functions
return separate keys.

Reused read-only: company-name compaction (suffixes, punctuation, &/AND, THE,
spacing, initials — trade words are kept), ROC license normalization (rejects
placeholders such as HTE-0001), city normalization, public-email-domain list,
placeholder names, and person-name detection.

Shadow-specific (and why):
  * phone — the shared ``normalize_phone`` keeps the last 10 digits of every
    digit in the string, so "623-773-7225 Option1" becomes a wrong number. The
    shadow parser reads the first well-formed NANP number instead.
  * address — no shared address normalizer exists.
  * DBA / AKA splitting — no shared parser exists.
  * code-like placeholders such as "AFP I-0869" seen in permit name fields.
"""

from __future__ import annotations

import re

from pipeline.company_resolution.normalize import (
    _PUBLIC_EMAIL_DOMAINS,
    is_placeholder_name,
    looks_like_company,
    normalize_city,
)
from pipeline.entity.names import GENERIC_TOKENS, compact_company_name
from pipeline.roc.normalize import looks_like_person_name, normalize_roc_license

# ---------------------------------------------------------------- names
_DBA_SPLIT = re.compile(
    r"\s+(?:D\s*/\s*B\s*/\s*A|D\.\s*B\.\s*A\.?|DBA|DOING\s+BUSINESS\s+AS|A\s*/\s*K\s*/\s*A|A\.\s*K\.\s*A\.?|AKA)"
    r"[\s:.,-]+", re.IGNORECASE)
_CODE_LIKE = re.compile(r"^[A-Z]{2,5}\s?[A-Z]?\s?-?\s?\d{3,}[A-Z]?$")
_HAS_LETTER = re.compile(r"[A-Za-z]")
_BUSINESS_WORDS = re.compile(
    r"\b(TRUST|ESTATE|PARTNERS|PARTNERSHIP|ASSOCIATES|ASSOCIATION|HOLDINGS|PROPERTIES|DEVELOPMENT|"
    r"COMPANY|COMPANIES|CORP|CORPORATION|INC|LLC|LLP|LP|LTD|PLLC|GROUP|CITY OF|TOWN OF|COUNTY|SCHOOL|"
    r"CHURCH|UNIVERSITY|DISTRICT|AUTHORITY|SOLAR|ENERGY|POOLS?|PLUMBING|ELECTRIC|MECHANICAL|ROOFING|"
    r"CONSTRUCTION|BUILDERS?|HOMES|SERVICES?|SYSTEMS|DESIGN|PERMIT|RENOVATIONS?|REMODEL(?:ING)?|LANDSCAP\w*|"
    r"CONCRETE|MASONRY|PAVING|EXCAVATING|GRADING|DRAIN(?:AGE)?|SEPTIC|SEWER|IRRIGATION|HVAC|AIR|HEATING|COOLING|"
    r"GLASS|GLAZING|TILE|FLOORING|PAINTING|DRYWALL|FRAMING|FENC\w*|WELDING|STEEL|IRON|CABINETS?|COUNTERTOPS?|"
    r"WINDOWS?|DOORS?|GARAGE|INSULATION|STUCCO|PLASTER|CONTRACTING|CONTRACTORS?|INDUSTRIES|ENTERPRISES?|"
    r"SOLUTIONS|TECHNOLOGIES|MANAGEMENT|INVESTMENTS?|CAPITAL|REALTY|ARCHITECTS?|ARCHITECTURE|ENGINEERING|"
    r"ELECTRICAL|PLUMBERS?|SUPPLY|RESTORATION|REPAIR|MAINTENANCE)\b")
_JOINT_PERSONS = re.compile(r"^[A-Z][A-Z'.-]*(?:\s+[A-Z][A-Z'.-]*){0,2}\s+(?:AND|&)\s+[A-Z][A-Z'.-]*(?:\s+[A-Z][A-Z'.-]*){0,3}$")


def clean_text(raw) -> str | None:
    if raw is None:
        return None
    s = " ".join(str(raw).replace("\r", " ").replace("\n", " ").split())
    return s or None


def is_placeholder(raw) -> bool:
    s = clean_text(raw)
    if not s or not _HAS_LETTER.search(s):
        return True
    if is_placeholder_name(s):
        return True
    return bool(_CODE_LIKE.match(s.upper()))


def split_dba(raw) -> tuple[str | None, str | None]:
    """'ABC Plumbing DBA ABC Mechanical' → ('ABC Plumbing', 'ABC Mechanical').
    Only explicit DBA/AKA markers split; both sides must contain letters."""
    s = clean_text(raw)
    if not s:
        return None, None
    parts = _DBA_SPLIT.split(s, maxsplit=1)
    if len(parts) != 2:
        return s, None
    legal, dba = parts[0].strip(" ,;-"), parts[1].strip(" ,;-")
    if not (_HAS_LETTER.search(legal or "") and _HAS_LETTER.search(dba or "")):
        return s, None
    return legal, dba


def name_key(raw) -> str | None:
    s = clean_text(raw)
    return compact_company_name(s) or None if s else None


def entity_kind(raw) -> str:
    """BUSINESS, PERSON, or UNKNOWN. Person-like ≠ invalid; it means 'needs
    business/license corroboration before it can be a company'."""
    s = clean_text(raw)
    if not s:
        return "UNKNOWN"
    up = s.upper()
    if looks_like_company(s) or _BUSINESS_WORDS.search(up):
        return "BUSINESS"
    if looks_like_person_name(s) or _JOINT_PERSONS.match(re.sub(r"[,]", "", up)):
        return "PERSON"
    return "UNKNOWN"


def distinctive(key: str | None) -> frozenset:
    if not key:
        return frozenset()
    return frozenset(t for t in key.split() if t not in GENERIC_TOKENS and len(t) > 1)


def names_agree(keys_a, keys_b) -> str | None:
    """'exact' when any legal/DBA key is identical; 'related' when one name's
    distinctive tokens are a strict subset of the other's (e.g. 'ABC PLUMBING'
    vs 'ABC PLUMBING AND MECHANICAL'). 'related' may only corroborate a strong
    identifier (license) — it never merges on its own."""
    a = {k for k in keys_a if k}
    b = {k for k in keys_b if k}
    if not a or not b:
        return None
    if a & b:
        return "exact"
    for x in a:
        dx = distinctive(x)
        if not dx:
            continue
        for y in b:
            dy = distinctive(y)
            if dy and (dx < dy or dy < dx) and len(dx & dy) >= 1:
                return "related"
    return None


# ---------------------------------------------------------------- contact
_NANP = re.compile(r"(?<!\d)(?:\+?1[\s.\-]?)?\(?([2-9]\d{2})\)?[\s.\-]?([2-9]\d{2})[\s.\-]?(\d{4})(?!\d)")


def phone_key(raw) -> str | None:
    if raw is None:
        return None
    m = _NANP.search(str(raw))
    return "".join(m.groups()) if m else None


def phone_from_parts(area, number) -> str | None:
    a = re.sub(r"\D", "", str(area or ""))
    n = re.sub(r"\D", "", str(number or ""))
    return phone_key(a + n) if len(a) == 3 and len(n) == 7 else None


def email_key(raw) -> str | None:
    s = (clean_text(raw) or "").lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[a-z]{2,}", s):
        return None
    return s


def domain_from_email(email) -> str | None:
    e = email_key(email)
    if not e:
        return None
    d = e.rsplit("@", 1)[1]
    return None if d in _PUBLIC_EMAIL_DOMAINS else d


def domain_from_url(raw) -> str | None:
    s = (clean_text(raw) or "").lower()
    if not s:
        return None
    s = re.sub(r"^[a-z]+://", "", s).split("/")[0].split("?")[0].split(":")[0]
    s = re.sub(r"^www\.", "", s)
    return s if re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", s) else None


def license_key(raw) -> str | None:
    return normalize_roc_license(raw)


def city_key(raw) -> str | None:
    return normalize_city(raw)


# ---------------------------------------------------------------- address
_ZIP5 = re.compile(r"(?<!\d)(\d{5})(?:-\d{4})?(?!\d)")
_UNIT = re.compile(r"\s(?:STE|SUITE|UNIT|APT|BLDG|BUILDING|SPC|SPACE|LOT|RM|ROOM|FL|FLOOR)\b.*$|\s#.*$")
_STREET_WORDS = {
    "STREET": "ST", "AVENUE": "AVE", "AV": "AVE", "ROAD": "RD", "DRIVE": "DR", "BOULEVARD": "BLVD",
    "LANE": "LN", "PARKWAY": "PKWY", "PLACE": "PL", "COURT": "CT", "HIGHWAY": "HWY", "CIRCLE": "CIR",
    "TRAIL": "TRL", "TERRACE": "TER", "NORTH": "N", "SOUTH": "S", "EAST": "E", "WEST": "W",
}
_PO_BOX = re.compile(r"^(?:P\s*O|POST OFFICE)\s*BOX\s*(\w+)")


def zip5(raw) -> str | None:
    if raw is None:
        return None
    m = _ZIP5.search(str(raw))
    return m.group(1) if m else None


def street_key(raw) -> str | None:
    """'1201 West Main Street, Ste 4' → '1201 W MAIN ST'; 'P.O. Box 10745' → 'PO BOX 10745'.
    A house number (or PO box number) is required."""
    s = clean_text(raw)
    if not s:
        return None
    s = s.upper().split(",")[0]
    s = re.sub(r"[.’']", "", s)
    m = _PO_BOX.match(s)
    if m:
        return f"PO BOX {m.group(1)}"
    s = re.sub(r"[^A-Z0-9# ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    s = _UNIT.sub("", " " + s).strip()
    tokens = [_STREET_WORDS.get(t, t) for t in s.split()]
    if len(tokens) < 2 or not tokens[0].isdigit():
        return None
    # A trailing bare unit number ('10032 W BELL RD 106') is dropped.
    if len(tokens) > 3 and tokens[-1].isdigit():
        tokens = tokens[:-1]
    return " ".join(tokens)


def street_from_parts(*parts) -> str | None:
    return street_key(" ".join(str(p) for p in parts if p not in (None, "")))
