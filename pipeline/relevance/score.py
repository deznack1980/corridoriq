"""Shadow customer-relevance score for a plumbing-supply profile.

Weights (internal, not customer-facing):
  45% permit trade demand
  30% intel-v2 contractor fit
  15% catalog scale (suppressed on low-demand jobs)
  10% timing / lifecycle

Does not write opportunity_score. Incidental trade labels cannot reach
the high band. GCs are not auto-excluded when the job has real demand.
"""

from __future__ import annotations

import math

from pipeline.analysis import constants as C
from pipeline.relevance.demand import demand_score

WEIGHT_DEMAND = 0.45
WEIGHT_FIT = 0.30
WEIGHT_SCALE = 0.15
WEIGHT_TIMING = 0.10

INCIDENTAL_RELEVANCE_CAP = 55.0

_CLASS_FIT = {
    "specialist_capability": 96.0,
    "recurring_trade_capability": 78.0,
    "incidental_project_scope": 16.0,
    "insufficient_evidence": 10.0,
}
_CLASS_RANK = {
    "specialist_capability": 4,
    "recurring_trade_capability": 3,
    "incidental_project_scope": 2,
    "insufficient_evidence": 1,
}
_TIMING = {
    "Application Submitted": 95.0,
    "Plan Review": 85.0,
    "Permit Issued": 75.0,
    "Construction Active": 55.0,
    "Inspection": 35.0,
    "Finaled": 15.0,
    "Closed": 15.0,
    "Unknown": 45.0,
}


def _best_trade(caps: list[dict]) -> dict | None:
    trades = [
        row for row in caps
        if row.get("capability") in {"plumbing", "fuel_gas"}
    ]
    if not trades:
        return None
    return max(
        trades,
        key=lambda row: (
            _CLASS_RANK.get(row.get("capability_class") or "", 0),
            float(row.get("confidence") or 0),
        ),
    )


def contractor_fit(
    caps: list[dict],
    demand: float,
) -> tuple[float, str, str | None, str | None]:
    """Return (fit 0-100, basis, attribution_role, capability_class)."""
    best = _best_trade(caps)
    by = {row.get("capability"): row for row in caps}
    hvac = by.get("hvac_mechanical")
    gc = by.get("general_contractor")
    role = (best or gc or hvac or (caps[0] if caps else {})).get("attribution_role")
    klass = (best or {}).get("capability_class")

    if best is not None:
        klass = best.get("capability_class") or "insufficient_evidence"
        role = best.get("attribution_role")
        fit = _CLASS_FIT.get(klass, 12.0)
        basis = f"{best['capability']}:{klass}"
        return fit, basis, role, klass

    hvac_class = (hvac or {}).get("capability_class")
    if (
        hvac is not None
        and hvac_class in {"specialist_capability", "recurring_trade_capability"}
        and demand >= 50
    ):
        return 52.0, "hvac_adjacent", hvac.get("attribution_role"), hvac_class

    if gc is not None or role == "gc_of_record":
        if demand >= 50:
            return 48.0, "gc_with_demand", role or "gc_of_record", (gc or {}).get("capability_class")
        return 22.0, "gc_low_demand", role or "gc_of_record", (gc or {}).get("capability_class")

    if not caps:
        if demand >= 50:
            return 22.0, "unclassified_with_demand", None, None
        return 12.0, "unclassified", None, None

    other = caps[0]
    return 10.0, f"other:{other.get('capability')}", other.get("attribution_role"), other.get("capability_class")


def catalog_scale(demand: float, estimated_material_value: float | None) -> float:
    if demand < 30:
        return 12.0
    if not estimated_material_value or estimated_material_value <= 0:
        return float(C.SCALE_NEUTRAL_SCORE)
    cap = float(C.SCALE_VALUATION_CAP)
    return round(min(100.0, 100.0 * math.log10(estimated_material_value + 1) / math.log10(cap)), 1)


def timing_score(lifecycle: str | None) -> float:
    return _TIMING.get((lifecycle or "").strip() or "Unknown", 45.0)


def score_project(
    *,
    permit_type: str | None,
    description: str | None,
    project_category: str | None,
    extra_text: str | None = None,
    capabilities: list[dict] | None = None,
    estimated_material_value: float | None = None,
    project_lifecycle: str | None = None,
) -> dict:
    demand, flags = demand_score(
        permit_type=permit_type,
        description=description,
        project_category=project_category,
        extra_text=extra_text,
    )
    fit, basis, role, klass = contractor_fit(capabilities or [], demand)
    scale = catalog_scale(demand, estimated_material_value)
    timing = timing_score(project_lifecycle)
    raw = (
        demand * WEIGHT_DEMAND
        + fit * WEIGHT_FIT
        + scale * WEIGHT_SCALE
        + timing * WEIGHT_TIMING
    )
    if klass == "incidental_project_scope":
        raw = min(raw, INCIDENTAL_RELEVANCE_CAP)
    return {
        "relevance_score": round(raw, 1),
        "demand_score": demand,
        "contractor_fit_score": round(fit, 1),
        "catalog_scale_score": round(scale, 1),
        "timing_score": round(timing, 1),
        "demand_flags": flags,
        "contractor_fit_basis": basis,
        "attribution_role": role,
        "capability_class": klass,
    }
