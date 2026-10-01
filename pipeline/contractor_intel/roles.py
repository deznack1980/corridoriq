"""Internal role attribution for contractor-capability evidence.

Separates the company that *does* a trade from the GC / owner / designer
listed on a project permit. Nothing here is customer-facing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from pipeline.contractor_intel.signals import normalize_text
from pipeline.contractor_intel.taxonomy import (
    CORE_PLUMBING_PHRASES,
    EVIDENCE_DIRECT,
    EVIDENCE_INDIRECT,
    GAS_PIPING_PHRASES,
    PRODUCTION_BUILDER_MARKERS,
    STRONG_GAS_PHRASES,
)

ROLE_TRADE = "trade_contractor"
ROLE_GC = "gc_of_record"
ROLE_SUB = "subcontractor_if_known"
ROLE_OWNER_BUILDER = "owner_builder"
ROLE_DEVELOPER = "developer"
ROLE_ARCH = "architect_engineer_if_present"
ROLE_UNKNOWN = "unknown_role"

KIND_PRODUCTION_BUILDER = "production_builder"
KIND_POOL = "pool"
KIND_LANDSCAPE = "landscape"
KIND_TRADE = "trade"
KIND_ARCH = "architect_engineer"
KIND_DEVELOPER = "developer"
KIND_OWNER_BUILDER = "owner_builder"
KIND_UNKNOWN = "unknown"

_HOMES = re.compile(r"\bhomes?\b")
_BUILDER = re.compile(r"\bbuilders?\b")
_HOME_BUILDER = re.compile(r"\bhome\s*builders?\b")
_CONSTRUCTION = re.compile(r"\bconstruction\b|\bcons(?:truction)?\s+co\b")
_POOL = re.compile(r"\bpools?\b|\bspas?\b|\baquatic\b|\bwatershape\b")
_LANDSCAPE = re.compile(
    r"\blandscap|\bhardscape\b|\birrigation\b|\bxeriscape\b"
)
_ARCH = re.compile(r"\barchitect|\barchitecture\b")
_ENGINEER_FIRM = re.compile(r"\bengineers?\b|\bengineering\b")
_DEVELOPER = re.compile(r"\bdevelopers?\b|\bdevelopment\b|\bland\s+co\b")
_OWNER_BUILDER = re.compile(r"\bowner\s*/?\s*builder\b|\bhomeowner\b")
_REMODEL = re.compile(r"\bremodel|\brenovat")
_TRADE_NAME = re.compile(
    r"\bplumb|\bgas\b|\bpropane\b|\bhvac\b|\bmechanical\b|\belectric"
    r"|\broof|\bconcrete\b|\bfram(?:e|ing)\b|\bdrywall\b|\bfloor"
    r"|\bcabinet|\butilit"
)
_CIVIL = re.compile(r"\bcivil\b|\butility\b|\bexcav")
_NEW_HOME = re.compile(
    r"\bnew\s+\d+\s*(?:sf|sq)\b|\b(?:two|single|one|1|2)\s*story\s+residence"
    r"|\bnew\s+(?:single[-\s]*family|sfr|residence|home)\b"
    r"|\bnew\s+\d{3,5}\s*(?:square\s*foot|sq\.?\s*ft)",
    re.IGNORECASE,
)
_INSTALL_GAS = re.compile(
    r"\binstall(?:ation)?\b.*\bgas\b|\bgas\s+line\b|\bgas\s+pip(?:e|ing)\b"
    r"|\bcsst\b|\bfrom\s+(?:the\s+)?gas\s+meter\b",
    re.IGNORECASE,
)
_SWIM_POOL = re.compile(
    r"\bswimming\s+pool\b|\bpool\s*(?:&|and)\s*spa\b|\bconstruct\s+a\s+\d+\s*sf\s+swimming",
    re.IGNORECASE,
)

_BUILDING_TYPES = frozenset(
    {
        "res",
        "residential",
        "residential building",
        "building residential",
        "building commercial",
        "building",
        "bld",
        "com",
        "commercial",
        "sfr",
        "sfr standard",
        "sfr custom in subdivision",
        "sfr detached garage",
        "guest house",
        "new construction",
    }
)
_BUILDING_PREFIXES = (
    "sfr",
    "building",
    "residential",
    "commercial",
)
_TRADE_TYPE_TOKENS = (
    "plumb",
    "plmb",
    "mechanical",
    "hvac",
    "mech",
    "electrical",
    "electric",
    "elec",
    "fuel gas",
    "water heater",
    "roof",
    "drywall",
    "framing",
    "concrete",
    "floor",
)
_POOL_TYPE_TOKENS = ("swimming pool", "pool w/spa", "pool spa", "spa")
_LANDSCAPE_TYPE_TOKENS = ("native plant", "landscape", "irrigation")
_UTIL_BLANKET = "util permit"


@dataclass(frozen=True)
class CompanyProfile:
    role: str
    role_confidence: float
    kind: str
    building_permit_share: float = 0.0
    trade_permit_share: float = 0.0
    pool_permit_count: int = 0
    total_permits: int = 0


def _norm_type(permit_type: str | None) -> str:
    return normalize_text(permit_type)


def is_building_permit(permit_type: str | None) -> bool:
    text = _norm_type(permit_type)
    if not text:
        return False
    if any(tok in text for tok in ("plumb", "electric", "mechanical", "hvac", "fuel gas")):
        return False
    if text in _BUILDING_TYPES:
        return True
    return any(text.startswith(prefix) for prefix in _BUILDING_PREFIXES)


def is_trade_permit(permit_type: str | None, *, capability: str | None = None) -> bool:
    text = _norm_type(permit_type)
    if not text:
        return False
    if _UTIL_BLANKET in text:
        return False
    if capability == "fuel_gas":
        return "fuel gas" in text or "water heater gas" in text or text in {"gas", "gas line"}
    if capability == "plumbing":
        return any(tok in text for tok in ("plumb", "plmb", "water heater"))
    return any(tok in text for tok in _TRADE_TYPE_TOKENS)


def is_pool_permit(permit_type: str | None, description: str | None = None) -> bool:
    text = _norm_type(permit_type)
    if any(tok in text for tok in _POOL_TYPE_TOKENS):
        return True
    blob = description or ""
    return bool(_SWIM_POOL.search(blob))


def is_new_home_description(description: str | None) -> bool:
    if not description:
        return False
    return bool(_NEW_HOME.search(description))


def is_gas_install_description(description: str | None) -> bool:
    if not description:
        return False
    return bool(_INSTALL_GAS.search(description))


def names_match(a: str | None, b: str | None) -> bool:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return False
    return na == nb or na in nb or nb in na


def _trade_name(name: str) -> bool:
    return bool(_TRADE_NAME.search(name))


def _production_builder_name(name: str) -> bool:
    if any(marker in name for marker in PRODUCTION_BUILDER_MARKERS):
        return True
    if _HOME_BUILDER.search(name):
        return True
    if _HOMES.search(name) and not _trade_name(name):
        return True
    return False


def infer_kind(
    *,
    company_name: str | None,
    permit_types: list[str] | None = None,
    descriptions: list[str] | None = None,
    company_role_types: list[str] | None = None,
) -> str:
    name = normalize_text(company_name)
    roles = {str(r).lower() for r in (company_role_types or [])}
    types = [_norm_type(t) for t in (permit_types or []) if t]
    total = len(types) or 1
    pool_n = sum(1 for t in types if any(tok in t for tok in _POOL_TYPE_TOKENS))
    landscape_n = sum(1 for t in types if any(tok in t for tok in _LANDSCAPE_TYPE_TOKENS))
    building_n = sum(1 for t in types if is_building_permit(t))
    desc_pool = sum(1 for d in (descriptions or []) if is_pool_permit(None, d))

    if _OWNER_BUILDER.search(name) or "owner builder" in roles:
        return KIND_OWNER_BUILDER
    if "architect" in roles or "engineer" in roles or _ARCH.search(name):
        return KIND_ARCH
    if _ENGINEER_FIRM.search(name) and not _trade_name(name) and building_n < 3:
        return KIND_ARCH
    if "developer" in roles or _DEVELOPER.search(name):
        return KIND_DEVELOPER
    if _production_builder_name(name):
        return KIND_PRODUCTION_BUILDER
    if _POOL.search(name) or pool_n >= 3 or (pool_n / total) >= 0.15 or desc_pool >= 3:
        return KIND_POOL
    if _LANDSCAPE.search(name) or landscape_n >= 5:
        return KIND_LANDSCAPE
    if _trade_name(name):
        return KIND_TRADE
    if (
        building_n >= 15
        and (building_n / total) >= 0.8
        and not _trade_name(name)
        and not _CIVIL.search(name)
    ):
        return KIND_PRODUCTION_BUILDER
    if (_BUILDER.search(name) or _CONSTRUCTION.search(name)) and (building_n / total) >= 0.7:
        if not _trade_name(name) and not _CIVIL.search(name):
            return KIND_PRODUCTION_BUILDER
    if _REMODEL.search(name) and not _trade_name(name):
        return KIND_PRODUCTION_BUILDER
    return KIND_UNKNOWN


def kind_to_role(kind: str) -> tuple[str, float]:
    mapping = {
        KIND_PRODUCTION_BUILDER: (ROLE_GC, 92.0),
        KIND_OWNER_BUILDER: (ROLE_OWNER_BUILDER, 88.0),
        KIND_DEVELOPER: (ROLE_DEVELOPER, 85.0),
        KIND_ARCH: (ROLE_ARCH, 88.0),
        KIND_POOL: (ROLE_TRADE, 80.0),
        KIND_LANDSCAPE: (ROLE_TRADE, 78.0),
        KIND_TRADE: (ROLE_TRADE, 86.0),
        KIND_UNKNOWN: (ROLE_UNKNOWN, 40.0),
    }
    return mapping.get(kind, (ROLE_UNKNOWN, 40.0))


def infer_profile(
    *,
    company_name: str | None,
    permit_types: list[str] | None = None,
    descriptions: list[str] | None = None,
    company_role_types: list[str] | None = None,
    owner_link_count: int = 0,
    contractor_link_count: int = 0,
) -> CompanyProfile:
    types = [t for t in (permit_types or []) if t]
    total = len(types)
    building_n = sum(1 for t in types if is_building_permit(t))
    trade_n = sum(1 for t in types if is_trade_permit(t))
    pool_n = sum(1 for t in types if is_pool_permit(t))
    kind = infer_kind(
        company_name=company_name,
        permit_types=types,
        descriptions=descriptions,
        company_role_types=company_role_types,
    )
    if (
        kind == KIND_UNKNOWN
        and owner_link_count
        and contractor_link_count
        and owner_link_count >= contractor_link_count
        and not _trade_name(normalize_text(company_name))
    ):
        kind = KIND_OWNER_BUILDER
    role, conf = kind_to_role(kind)
    if kind == KIND_UNKNOWN and trade_n and trade_n >= building_n:
        role, conf, kind = ROLE_TRADE, 70.0, KIND_TRADE
    share_b = (building_n / total) if total else 0.0
    share_t = (trade_n / total) if total else 0.0
    return CompanyProfile(
        role=role,
        role_confidence=conf,
        kind=kind,
        building_permit_share=round(share_b, 3),
        trade_permit_share=round(share_t, 3),
        pool_permit_count=pool_n,
        total_permits=total,
    )


def attribute_observation(
    *,
    profile: CompanyProfile,
    permit_type: str | None,
    description: str | None,
    company_name: str | None,
    general_contractor_name: str | None,
    plumbing_contractor_name: str | None,
    capability: str,
    signal_value: str,
    signal_type: str,
) -> tuple[str, float, str]:
    """Return (attribution_role, attribution_confidence, evidence_directness)."""
    value = str(signal_value or "").strip().lower()
    trade_permit = is_trade_permit(permit_type, capability=capability)
    building = is_building_permit(permit_type)
    pool = profile.kind == KIND_POOL or is_pool_permit(permit_type, description)
    new_home = is_new_home_description(description)
    gc_named = names_match(general_contractor_name, company_name)
    plum_named = names_match(plumbing_contractor_name, company_name)

    if plum_named and general_contractor_name and not gc_named:
        return ROLE_SUB, 78.0, EVIDENCE_DIRECT if trade_permit else EVIDENCE_INDIRECT

    if profile.role == ROLE_ARCH:
        return ROLE_ARCH, profile.role_confidence, EVIDENCE_INDIRECT
    if profile.role == ROLE_DEVELOPER:
        return ROLE_DEVELOPER, profile.role_confidence, EVIDENCE_INDIRECT
    if profile.role == ROLE_OWNER_BUILDER:
        return ROLE_OWNER_BUILDER, profile.role_confidence, EVIDENCE_INDIRECT

    strong_gas = capability == "fuel_gas" and (
        value in GAS_PIPING_PHRASES
        or (value in STRONG_GAS_PHRASES and is_gas_install_description(description))
    )
    core_plumbing = capability == "plumbing" and value in CORE_PLUMBING_PHRASES

    if profile.role == ROLE_GC or profile.kind == KIND_PRODUCTION_BUILDER:
        if trade_permit and not new_home:
            return ROLE_TRADE, 72.0, EVIDENCE_DIRECT
        return ROLE_GC, max(profile.role_confidence, 80.0), EVIDENCE_INDIRECT

    if pool and capability in {"plumbing", "fuel_gas"}:
        if capability == "fuel_gas" and strong_gas:
            return ROLE_TRADE, 76.0, EVIDENCE_DIRECT
        return ROLE_TRADE, 70.0, EVIDENCE_INDIRECT

    if trade_permit:
        return ROLE_TRADE, 84.0, EVIDENCE_DIRECT
    if signal_type == "permit_type" and capability in {"plumbing", "fuel_gas"}:
        return ROLE_TRADE, 80.0, EVIDENCE_DIRECT
    if strong_gas or core_plumbing:
        directness = EVIDENCE_INDIRECT if (building and new_home) else EVIDENCE_DIRECT
        role = ROLE_GC if (building and new_home) else ROLE_TRADE
        return role, 74.0, directness
    if building or gc_named:
        if profile.role == ROLE_TRADE:
            return ROLE_TRADE, 68.0, EVIDENCE_INDIRECT
        return ROLE_GC, 62.0, EVIDENCE_INDIRECT
    if profile.role == ROLE_TRADE:
        return ROLE_TRADE, profile.role_confidence, EVIDENCE_INDIRECT
    return ROLE_UNKNOWN, 40.0, EVIDENCE_INDIRECT
