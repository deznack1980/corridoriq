"""Account trade identity for plumbing-supply priority.

Reads intel-v2 rows but does not rewrite them. A company that appears as
contractor-of-record on many PLMB permits is not automatically a plumbing
specialist when name and capability mix look like GC / CM / builder.

Reserved license-registry payloads (disabled) can later confirm or
challenge this identity. They are not ingested here.
"""

from __future__ import annotations

import json
import re
import sqlite3

from pipeline.contractor_intel.signals import normalize_text

ID_PLUMBING_SPECIALIST = "plumbing_specialist"
ID_FUEL_GAS_SPECIALIST = "fuel_gas_specialist"
ID_RECURRING_PLUMBING = "recurring_plumbing"
ID_RECURRING_FUEL_GAS = "recurring_fuel_gas"
ID_MECHANICAL_WET = "mechanical_wet"
ID_SITE_UTILITY = "site_utility"
ID_GC_DEMAND = "gc_with_plumbing_demand"
ID_INCIDENTAL = "incidental_trade"
ID_OTHER_TRADE = "other_trade"
ID_MUNICIPALITY = "municipality"
ID_UNKNOWN = "unknown"

IDENTITY_SCORE = {
    ID_PLUMBING_SPECIALIST: 96.0,
    ID_FUEL_GAS_SPECIALIST: 94.0,
    ID_RECURRING_PLUMBING: 82.0,
    ID_RECURRING_FUEL_GAS: 80.0,
    ID_MECHANICAL_WET: 68.0,
    ID_SITE_UTILITY: 64.0,
    ID_GC_DEMAND: 42.0,
    ID_OTHER_TRADE: 28.0,
    ID_INCIDENTAL: 16.0,
    ID_MUNICIPALITY: 6.0,
    ID_UNKNOWN: 10.0,
}

_TRADE_NAME = re.compile(
    r"\bplumb|\bgas\b|\bpropane\b|\bpipe(?:line)?\b|\bmechanical\b|"
    r"\bhvac\b|\brooter\b|\bbackflow\b|\bunderground\b|\butility\b|"
    r"\bsewer\b|\bwater works\b"
)
_GAS_NAME = re.compile(r"\bgas\b|\bpropane\b|\bamerigas\b|\bferrell\b")
_ELECTRICAL_NAME = re.compile(r"\belectri")
_FIRE_NAME = re.compile(
    r"\bfire protection\b|\bfire prev|\bfire control\b|\bfire equipment\b|"
    r"\bsprinkler\b|\bfireshield\b"
)
_POOL_NAME = re.compile(r"\bpools?\b|\bspas?\b")
_GC_CM = re.compile(
    r"\bcommercial\b|\bconstruction\b|\bbuilders?\b|\bkitchell\b|"
    r"\bdesign[-\s]?build\b|\bintegrated services\b|\bgeneral contract|"
    r"\bcommunities\b|\bhomes?\b"
)
_MUNI = re.compile(
    r"\bcity of\b|\btown of\b|\bcounty of\b|\bwater services\b|"
    r"\bmunicipal\b|\bdepartment of\b"
)
_OWNER_OCC = re.compile(r"\bhospital\b|\bclinic\b|\buniversity\b|\bschool district\b")


def _cap(caps: list[dict], name: str) -> dict | None:
    return next((c for c in caps if c.get("capability") == name), None)


def _class(caps: list[dict], name: str) -> str | None:
    row = _cap(caps, name)
    return None if row is None else row.get("capability_class")


def looks_like_municipality(name: str | None) -> bool:
    return bool(_MUNI.search(normalize_text(name)))


def looks_like_gc_cm(name: str | None, caps: list[dict]) -> bool:
    n = normalize_text(name)
    if not n or looks_like_municipality(n):
        return False
    trade_name = bool(_TRADE_NAME.search(n))
    if trade_name:
        return False
    if _GC_CM.search(n):
        return True
    roles = {c.get("attribution_role") for c in caps}
    if "gc_of_record" in roles or "developer" in roles:
        return True
    return False


def reserved_license_payload(conn: sqlite3.Connection, company_id: int) -> dict | None:
    """Return a reserved license-registry payload if one is ever stored.

    ROC / ACC families stay disabled. This is the future join point; it
    must not ingest anything itself.
    """
    enabled = conn.execute(
        "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='roc'"
    ).fetchone()
    if not enabled or int(enabled[0] or 0) != 1:
        return None
    row = conn.execute(
        """
        SELECT payload_json, confidence FROM company_enrichment
        WHERE company_id=? AND source_family='roc'
        ORDER BY observed_at DESC LIMIT 1
        """,
        (company_id,),
    ).fetchone()
    if row is None or not row["payload_json"]:
        return None
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None
    payload["_confidence"] = row["confidence"]
    return payload


def infer_trade_identity(
    *,
    company_name: str | None,
    caps: list[dict],
    license_payload: dict | None = None,
) -> dict:
    """Return identity label, score, basis, and confidence (0-100)."""
    n = normalize_text(company_name)
    plum = _cap(caps, "plumbing")
    gas = _cap(caps, "fuel_gas")
    hvac = _cap(caps, "hvac_mechanical")
    site = _cap(caps, "site_utility")
    role = None
    for row in (plum, gas, hvac, site):
        if row and row.get("attribution_role"):
            role = row["attribution_role"]
            break
    if caps and not role:
        role = caps[0].get("attribution_role")

    basis = "permit_behavior"
    # Future: license classification can confirm plumbing vs GC without
    # becoming the sole priority driver.
    if license_payload:
        basis = "permit_behavior+license_registry"

    if looks_like_municipality(n) or _OWNER_OCC.search(n):
        return {
            "trade_identity": ID_MUNICIPALITY,
            "trade_identity_score": IDENTITY_SCORE[ID_MUNICIPALITY],
            "confidence": 88.0,
            "identity_basis": basis,
            "role": role or "unknown_role",
        }
    if looks_like_gc_cm(company_name, caps):
        return {
            "trade_identity": ID_GC_DEMAND,
            "trade_identity_score": IDENTITY_SCORE[ID_GC_DEMAND],
            "confidence": 72.0,
            "identity_basis": basis,
            "role": role or "gc_of_record",
        }
    if _FIRE_NAME.search(n):
        return _pack(ID_OTHER_TRADE, role, basis, 78.0)
    if _POOL_NAME.search(n) and not re.search(r"\bplumb", n):
        return _pack(ID_INCIDENTAL, role, basis, 82.0)
    if _ELECTRICAL_NAME.search(n) and not _TRADE_NAME.search(n):
        return {
            "trade_identity": ID_OTHER_TRADE,
            "trade_identity_score": IDENTITY_SCORE[ID_OTHER_TRADE],
            "confidence": 70.0,
            "identity_basis": basis,
            "role": role or "trade_contractor",
        }

    plum_class = _class(caps, "plumbing")
    gas_class = _class(caps, "fuel_gas")
    site_class = _class(caps, "site_utility")
    hvac_class = _class(caps, "hvac_mechanical")
    trade_name = bool(_TRADE_NAME.search(n))

    if gas_class == "specialist_capability" and _GAS_NAME.search(n):
        return _pack(ID_FUEL_GAS_SPECIALIST, role, basis, 92.0)
    if plum_class == "specialist_capability" and (trade_name or (plum and plum.get("confidence", 0) >= 75)):
        if trade_name or (plum and not looks_like_gc_cm(company_name, caps)):
            conf = 90.0 if trade_name else 74.0
            return _pack(ID_PLUMBING_SPECIALIST, role, basis, conf)
    if gas_class == "specialist_capability":
        return _pack(ID_FUEL_GAS_SPECIALIST, role, basis, 88.0 if trade_name else 72.0)
    if plum_class == "recurring_trade_capability":
        return _pack(ID_RECURRING_PLUMBING, role, basis, 78.0)
    if gas_class == "recurring_trade_capability":
        return _pack(ID_RECURRING_FUEL_GAS, role, basis, 76.0)
    if site_class in {"specialist_capability", "recurring_trade_capability"}:
        return _pack(ID_SITE_UTILITY, role, basis, 70.0)
    if hvac_class in {"specialist_capability", "recurring_trade_capability"}:
        return _pack(ID_MECHANICAL_WET, role, basis, 66.0)
    if plum_class == "incidental_project_scope" or gas_class == "incidental_project_scope":
        return _pack(ID_INCIDENTAL, role, basis, 80.0)
    if not caps:
        return _pack(ID_UNKNOWN, role, basis, 30.0)
    return _pack(ID_UNKNOWN, role, basis, 40.0)


def _pack(label: str, role: str | None, basis: str, confidence: float) -> dict:
    return {
        "trade_identity": label,
        "trade_identity_score": IDENTITY_SCORE[label],
        "confidence": confidence,
        "identity_basis": basis,
        "role": role or "unknown_role",
    }
