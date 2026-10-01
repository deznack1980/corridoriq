"""Evidence-based confidence for a single capability on one company.

A single weak hit is not treated as proof. Generic ``gas`` tokens do not
raise fuel-gas confidence. GC / owner / designer project-scope mentions
are capped as incidental. Repeat observations and mixed signal types
raise confidence. Output is 0-100.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from pipeline.contractor_intel.roles import (
    KIND_LANDSCAPE,
    KIND_POOL,
    KIND_PRODUCTION_BUILDER,
    KIND_TRADE,
    CompanyProfile,
)
from pipeline.contractor_intel.taxonomy import (
    CORE_PLUMBING_PHRASES,
    EVIDENCE_DIRECT,
    GC_INCIDENTAL_CAP,
    GENERIC_GAS_PHRASE,
    LANDSCAPE_PLUMBING_CAP,
    LIFESTYLE_GAS_CAP,
    NAME_ONLY_CAP,
    NON_TRADE_ROLES,
    POOL_PLUMBING_CAP,
    SIGNAL_COMPANY_NAME,
    STRONG_GAS_PHRASES,
    GAS_PIPING_PHRASES,
    TRADE_CAPABILITIES,
)


@dataclass(frozen=True)
class Evidence:
    capability: str
    signal_type: str
    signal_value: str
    weight: float
    permit_id: int | None = None
    project_id: int | None = None
    evidence_date: str | None = None
    attribution_role: str = "unknown_role"
    attribution_confidence: float = 0.0
    evidence_directness: str = "indirect"


def _usable(evidence: Sequence[Evidence]) -> list[Evidence]:
    out: list[Evidence] = []
    for row in evidence:
        value = str(row.signal_value).strip().lower()
        if row.capability == "fuel_gas" and value == GENERIC_GAS_PHRASE:
            continue
        if row.weight <= 0:
            continue
        out.append(row)
    return out


def compute_confidence(
    evidence: Sequence[Evidence],
    *,
    profile: CompanyProfile | None = None,
) -> float:
    usable = _usable(evidence)
    if not usable:
        return 0.0

    capability = usable[0].capability
    types = {row.signal_type for row in usable}
    permits = {row.permit_id for row in usable if row.permit_id is not None}
    max_weight = max(row.weight for row in usable)
    raw = sum(row.weight for row in usable)
    score = 100.0 * (1.0 - math.exp(-raw / 55.0))

    if len(permits) >= 5:
        score += 12
    elif len(permits) >= 3:
        score += 8
    elif len(permits) >= 2:
        score += 4

    if len(types) >= 3:
        score += 8
    elif len(types) >= 2:
        score += 6

    if max_weight >= 22:
        score += 5

    if types <= {SIGNAL_COMPANY_NAME}:
        score = min(score, NAME_ONLY_CAP)

    values = {str(row.signal_value).strip().lower() for row in usable}
    has_strong_gas = bool(values & STRONG_GAS_PHRASES)
    if capability == "fuel_gas" and not has_strong_gas:
        score = min(score, LIFESTYLE_GAS_CAP)

    if len(usable) == 1 and max_weight < 16:
        score = min(score, 32.0)

    direct = [row for row in usable if row.evidence_directness == EVIDENCE_DIRECT]
    if profile is not None and capability in TRADE_CAPABILITIES:
        if profile.kind == KIND_PRODUCTION_BUILDER:
            score = min(score, GC_INCIDENTAL_CAP)
        elif profile.role in NON_TRADE_ROLES:
            if not direct:
                score = min(score, GC_INCIDENTAL_CAP)
        if profile.kind in {KIND_POOL, KIND_LANDSCAPE} and capability == "plumbing":
            if not (values & CORE_PLUMBING_PHRASES) or not direct:
                cap = POOL_PLUMBING_CAP if profile.kind == KIND_POOL else LANDSCAPE_PLUMBING_CAP
                score = min(score, cap)
        if profile.kind in {KIND_POOL, KIND_LANDSCAPE} and capability == "fuel_gas":
            if not (values & GAS_PIPING_PHRASES):
                score = min(score, LIFESTYLE_GAS_CAP)

    return round(min(100.0, max(0.0, score)), 1)


def capability_class(
    score: float,
    evidence: Sequence[Evidence],
    *,
    profile: CompanyProfile | None = None,
) -> str:
    usable = _usable(evidence)
    if score < 20 or not usable:
        return "insufficient_evidence"
    capability = usable[0].capability
    values = {str(row.signal_value).strip().lower() for row in usable}
    direct = [row for row in usable if row.evidence_directness == EVIDENCE_DIRECT]
    permits = {row.permit_id for row in usable if row.permit_id is not None}

    if profile is not None:
        if profile.kind == KIND_PRODUCTION_BUILDER and capability in TRADE_CAPABILITIES:
            return "incidental_project_scope"
        if profile.role in NON_TRADE_ROLES:
            if capability in TRADE_CAPABILITIES and not direct:
                return "incidental_project_scope"
        if profile.kind in {KIND_POOL, KIND_LANDSCAPE}:
            if capability == "plumbing" and not (values & CORE_PLUMBING_PHRASES and direct):
                return "incidental_project_scope"
            if capability == "fuel_gas":
                if values & GAS_PIPING_PHRASES and len(permits) >= 2:
                    return "recurring_trade_capability"
                return "incidental_project_scope"

    if score < 35:
        return "insufficient_evidence"
    trade_identity = profile is not None and profile.kind == KIND_TRADE
    trade_role = profile is not None and profile.role == "trade_contractor"
    if score >= 75 and (trade_identity or (trade_role and len(permits) >= 5)):
        return "specialist_capability"
    if score >= 50 and (trade_role or trade_identity or len(direct) >= 2):
        return "recurring_trade_capability"
    if score >= 50:
        return "incidental_project_scope"
    return "insufficient_evidence"


def dominant_source(evidence: Sequence[Evidence]) -> str:
    usable = _usable(evidence) or list(evidence)
    if not usable:
        return "none"
    best = max(usable, key=lambda row: row.weight)
    return best.signal_type
