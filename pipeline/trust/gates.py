"""Fail-closed trust gates. Pure functions over loaded evidence.

Account priority is never a gate. It only orders records that already
passed, and it is shown so the reader can see it did not decide anything.
"""

from __future__ import annotations

import re

from pipeline.trust import contract as C
from pipeline.trust import scope as S
from pipeline.trust.health import FRESH, LAGGING

CALL_WINDOW_DAYS = 30
REVIEW_WINDOW_DAYS = 60

# Role vocabulary for the company on this permit.
TRADE_CONTRACTOR = "TRADE_CONTRACTOR"
GC = "GENERAL_CONTRACTOR"
OWNER_BUILDER = "OWNER_BUILDER"
DEVELOPER = "DEVELOPER"
DESIGNER = "ARCHITECT_ENGINEER"
PERMIT_SERVICE = "PERMIT_SERVICE"
NOT_SALES_READY = "NOT_SALES_READY"
MUNICIPALITY = "MUNICIPALITY"
UNKNOWN_ROLE = "UNKNOWN_ROLE"

_CAP_ROLE = {
    "trade_contractor": TRADE_CONTRACTOR,
    "subcontractor_if_known": TRADE_CONTRACTOR,
    "gc_of_record": GC,
    "owner_builder": OWNER_BUILDER,
    "developer": DEVELOPER,
    "architect_engineer_if_present": DESIGNER,
}
_LANE_CAPABILITY = {
    "PLUMBING_CORE": ("plumbing",),
    "FUEL_GAS_PROPANE": ("fuel_gas", "plumbing"),
    "FIRE_BACKFLOW": ("fire_protection", "plumbing"),
    "CIVIL_WET_UTILITY": ("site_utility",),
    "HVAC_MECHANICAL": ("hvac_mechanical",),
}
_EXPEDITE = re.compile(r"\bpermit service|\bpermit runner|\bexpedit|\bpermits? (?:plus|pros?|inc)\b", re.I)
_BOOK_FITS = {"HIGH", "MEDIUM"}

VERIFIED = "VERIFIED"
HIGH_CONFIDENCE = "HIGH_CONFIDENCE"
POSSIBLE = "POSSIBLE"
CONFLICT = "CONFLICT"
UNRESOLVED = "UNRESOLVED"
_ROC_STATUS = {
    "VERIFIED_MATCH": VERIFIED,
    "HIGH_CONFIDENCE_MATCH": HIGH_CONFIDENCE,
    "POSSIBLE_MATCH": POSSIBLE,
    "CONFLICT": CONFLICT,
}

VERIFIED_PHONE = "VERIFIED_PHONE"
VERIFIED_EMAIL = "VERIFIED_EMAIL"
PEER_ONLY = "PEER_INHERITED_ONLY"
CANDIDATE_ONLY = "CANDIDATE_ONLY"
WEB_ONLY = "WEB_OR_FORM_ONLY"
NO_CONTACT = "NONE"

# Soft flags that cap confidence at MEDIUM without blocking.
_CONFIDENCE_CAPS = {
    "SOURCE_LAGGING",
    "REFRESH_AGING",
    "DUPLICATE_NAME_PEER_UNMERGED",
    "IDENTITY_OVERRIDE_NOT_APPLIED",
    "ROLE_CONFIDENCE_BELOW_75",
}


def _compact(name: str | None) -> str:
    try:
        from pipeline.entity.names import compact_company_name

        return compact_company_name(name)
    except Exception:  # pragma: no cover
        return re.sub(r"[^A-Z0-9 ]", "", str(name or "").upper()).strip()


def _names_match(a: str | None, b: str | None) -> bool:
    ca, cb = _compact(a), _compact(b)
    if not ca or not cb:
        return False
    return ca == cb or (len(cb) >= 6 and cb in ca) or (len(ca) >= 6 and ca in cb)


def book_lanes(ctx: dict) -> list[str]:
    return sorted(
        {
            r["lane_key"]
            for r in ctx.get("lanes") or []
            if r.get("fit") in _BOOK_FITS and int(r.get("presentable") or 0) and r["lane_key"] != "IDENTITY_REVIEW"
        }
    )


def identity_state(ctx: dict) -> dict:
    flags: list[str] = []
    review = ctx.get("identity_review")
    statuses = {_ROC_STATUS.get(r.get("match_status"), None) for r in ctx.get("roc") or []}
    statuses.discard(None)
    classes = sorted(
        {(r.get("normalized_class") or r.get("raw_class") or "").strip() for r in ctx.get("roc") or []} - {""}
    )
    if review and review.get("recommended_identity_status"):
        status = review["recommended_identity_status"]
        basis = "curated identity review"
    elif CONFLICT in statuses:
        status, basis = CONFLICT, "ROC match marked CONFLICT"
    elif VERIFIED in statuses:
        status, basis = VERIFIED, "ROC VERIFIED_MATCH"
    elif HIGH_CONFIDENCE in statuses:
        status, basis = HIGH_CONFIDENCE, "ROC HIGH_CONFIDENCE_MATCH"
    elif POSSIBLE in statuses:
        status, basis = POSSIBLE, "ROC POSSIBLE_MATCH only"
    else:
        status, basis = UNRESOLVED, "no ROC match"
    for o in ctx.get("overrides") or []:
        original = o.get("original_match_status") or "NO_MATCH"
        recommended = o.get("recommended_match_status") or "NO_MATCH"
        if not int(o.get("applied") or 0) and recommended != original:
            flags.append("IDENTITY_OVERRIDE_NOT_APPLIED")
            break
    if ctx.get("name_peers"):
        flags.append("DUPLICATE_NAME_PEER_UNMERGED")
    if ctx.get("canonical_peers"):
        flags.append("CANONICAL_PEERS_EXIST")
    return {
        "status": status,
        "basis": basis,
        "roc_classes": classes,
        "canonical": (ctx.get("canonical") or {}).get("canonical_name"),
        "canonical_relationship": (ctx.get("canonical") or {}).get("relationship_type"),
        "canonical_peer_company_ids": ctx.get("canonical_peers") or [],
        "name_peer_company_ids": ctx.get("name_peers") or [],
        "flags": flags,
    }


def role_for(ctx: dict, permit: dict, lane: str | None) -> dict:
    company = ctx.get("company") or {}
    name = company.get("display_name") or ""
    priority = ctx.get("priority") or {}
    lanes = ctx.get("lanes") or []
    if _EXPEDITE.search(name):
        return {"role": PERMIT_SERVICE, "basis": "company name reads as a permit service", "confidence": 90.0, "level": "name"}
    if priority.get("trade_identity") == "municipality":
        return {"role": MUNICIPALITY, "basis": "trade identity municipality", "confidence": 88.0, "level": "account"}
    if lanes and not book_lanes(ctx) and any(r["lane_key"] == "IDENTITY_REVIEW" for r in lanes):
        return {
            "role": NOT_SALES_READY,
            "basis": "sales-lane assignment put this record in IDENTITY_REVIEW (applicant, owner-builder, or person name)",
            "confidence": 80.0,
            "level": "account",
        }
    plumb_named = _names_match(name, permit.get("plumbing_contractor_name"))
    gc_named = _names_match(name, permit.get("general_contractor_name"))
    if plumb_named:
        return {"role": TRADE_CONTRACTOR, "basis": "permit names this company as the plumbing contractor", "confidence": 90.0, "level": "permit"}
    other_plumber = (permit.get("plumbing_contractor_name") or "").strip()
    if gc_named and other_plumber:
        return {
            "role": GC,
            "basis": "permit names this company as general contractor and a different plumbing contractor",
            "confidence": 88.0,
            "level": "permit",
        }
    if priority.get("trade_identity") == "gc_with_plumbing_demand" or (
        "GENERAL_CONTRACTOR_CM" in book_lanes(ctx) and "PLUMBING_CORE" not in book_lanes(ctx)
    ):
        return {
            "role": GC,
            "basis": "account classified as a GC with plumbing demand, not a plumbing contractor",
            "confidence": 72.0,
            "level": "account",
        }
    caps = {c.get("capability"): c for c in ctx.get("capabilities") or []}
    for cap_name in _LANE_CAPABILITY.get(lane or "", ()):
        cap = caps.get(cap_name)
        if cap and cap.get("attribution_role"):
            role = _CAP_ROLE.get(cap["attribution_role"], UNKNOWN_ROLE)
            return {
                "role": role,
                "basis": f"account-level {cap_name} capability role {cap['attribution_role']} (from permit history, not this permit)",
                "confidence": float(cap.get("attribution_confidence") or cap.get("confidence") or 0),
                "level": "account",
            }
    return {"role": UNKNOWN_ROLE, "basis": "no permit-level or capability-level role evidence for this lane", "confidence": 0.0, "level": "none"}


def contact_state(ctx: dict) -> dict:
    usable = [
        c for c in ctx.get("contacts") or []
        if int(c.get("public_business_contact") or 0) and c.get("verification_status") in {"VERIFIED", "CANDIDATE"}
    ]

    def is_phone(c):
        return c.get("contact_type") == "business_phone" or (
            c.get("contact_type") == "office" and str(c.get("normalized_value") or "").isdigit()
        )

    def is_email(c):
        return c.get("contact_type") in {"business_email", "estimator", "purchasing", "office"} and "@" in str(
            c.get("contact_value") or ""
        )

    own = [c for c in usable if c.get("verification_status") == "VERIFIED" and c.get("source_family") != "canonical_peer"]
    peer = [c for c in usable if c.get("verification_status") == "VERIFIED" and c.get("source_family") == "canonical_peer"]
    named = next((c for c in own if c.get("contact_name") and c.get("contact_type") != "qualifying_party"), None)
    if any(is_phone(c) for c in own):
        state, pick = VERIFIED_PHONE, next(c for c in own if is_phone(c))
    elif any(is_email(c) for c in own):
        state, pick = VERIFIED_EMAIL, next(c for c in own if is_email(c))
    elif any(is_phone(c) or is_email(c) for c in peer):
        state, pick = PEER_ONLY, next(c for c in peer if is_phone(c) or is_email(c))
    elif any(c.get("contact_type") in {"website", "contact_form"} for c in own):
        state, pick = WEB_ONLY, next(c for c in own if c.get("contact_type") in {"website", "contact_form"})
    elif usable:
        state, pick = CANDIDATE_ONLY, usable[0]
    else:
        state, pick = NO_CONTACT, None
    return {
        "state": state,
        "channel_id": None if pick is None else pick.get("id"),
        "channel_type": None if pick is None else pick.get("contact_type"),
        "channel_source": None if pick is None else pick.get("source_family"),
        "verified_at": None if pick is None else pick.get("verified_at"),
        "named_contact": None if named is None else named.get("contact_name"),
        "named_title": None if named is None else (named.get("title") or None),
        "own_verified_channels": len(own),
        "peer_inherited_channels": len(peer),
        "note": (
            "Contact is verified only on a canonical peer record, not on this company row."
            if state == PEER_ONLY
            else None
        ),
    }


def evaluate(
    *,
    ctx: dict,
    lane: str | None,
    scope: dict,
    activity_days: int | None,
    source: dict,
    refresh_state: str,
    role: dict,
    identity: dict,
    contact: dict,
    prior_outcome: dict | None = None,
) -> dict:
    """Return action, confidence, blockers, and per-gate results."""
    blockers: list[tuple[str, str, str]] = []  # (gate, action, reason)
    flags: list[str] = list(identity.get("flags") or [])
    gates: dict[str, dict] = {}

    def gate(name: str, ok: bool, reason: str, action: str | None = None):
        gates[name] = {"pass": ok, "reason": reason}
        if not ok and action:
            blockers.append((name, action, reason))

    company = ctx.get("company") or {}
    active = (company.get("lifecycle_state") in {None, "active"}) and not company.get("merged_into_id")
    gate("company_active", active, "active company row" if active else "company row is merged or inactive", C.HOLD)

    fresh = source.get("freshness") in {FRESH, LAGGING} and not source.get("call_blocked")
    gate(
        "source_fresh",
        fresh,
        f"{source.get('jurisdiction')} newest record {source.get('newest_record_date')} "
        f"({source.get('days_since_newest_record')} days), run health {source.get('run_health')}",
        C.HOLD,
    )
    if source.get("freshness") == LAGGING:
        flags.append("SOURCE_LAGGING")

    refresh_ok = refresh_state in {"CURRENT", "AGING"}
    gate("refresh_recent", refresh_ok, f"latest successful refresh state {refresh_state}", C.HOLD)
    if refresh_state == "AGING":
        flags.append("REFRESH_AGING")

    recent = activity_days is not None and activity_days <= CALL_WINDOW_DAYS
    gate(
        "activity_recent",
        recent,
        f"relevant activity {activity_days} days ago (call window {CALL_WINDOW_DAYS})",
        C.RESEARCH,
    )

    callable_lane = lane in S.CALLABLE_LANES
    gate("lane_callable", callable_lane, f"lane {lane}", C.RESEARCH)

    in_book = lane in book_lanes(ctx)
    gate(
        "lane_in_account_book",
        in_book,
        f"account book lanes {book_lanes(ctx) or 'none'}",
        C.RESEARCH,
    )

    explicit = bool(scope.get("explicit"))
    gate(
        "scope_stated",
        explicit,
        "scope stated in permit description" if explicit else "scope inferred from permit type only",
        C.RESEARCH,
    )

    r = role.get("role")
    if r in {PERMIT_SERVICE, NOT_SALES_READY, MUNICIPALITY}:
        gate("contractor_role", False, role.get("basis", ""), C.DO_NOT_CONTACT)
    elif r == GC:
        gate("contractor_role", False, "GC association is not a plumbing trade contractor: " + role.get("basis", ""), C.RESEARCH)
    elif r in {UNKNOWN_ROLE, OWNER_BUILDER, DEVELOPER, DESIGNER}:
        gate("contractor_role", False, role.get("basis", ""), C.VERIFY_PROJECT_ROLE)
    else:
        gate("contractor_role", True, role.get("basis", ""))
        if role.get("level") == "account":
            flags.append("ROLE_FROM_ACCOUNT_HISTORY")
        if float(role.get("confidence") or 0) < 75:
            flags.append("ROLE_CONFIDENCE_BELOW_75")

    ident = identity.get("status")
    gate(
        "identity",
        ident in {VERIFIED, HIGH_CONFIDENCE},
        f"identity {ident} ({identity.get('basis')})",
        C.VERIFY_CONTRACTOR,
    )

    cs = contact.get("state")
    if cs in {VERIFIED_PHONE, VERIFIED_EMAIL}:
        gate("contact", True, f"{cs} on this company row")
    elif cs == PEER_ONLY:
        gate("contact", False, "verified channel exists only on a canonical peer record", C.VERIFY_CONTRACTOR)
    else:
        gate("contact", False, f"no verified phone or email on this company row ({cs})", C.RESEARCH)

    if prior_outcome and prior_outcome.get("outcome") in C.NEGATIVE_DATA_OUTCOMES:
        gate(
            "prior_outcome",
            False,
            f"latest human outcome {prior_outcome['outcome']} on {prior_outcome.get('recorded_at')}",
            C.HOLD,
        )

    if blockers:
        action = min((b[1] for b in blockers), key=lambda a: C.ACTION_SEVERITY[a])
        confidence = C.MEDIUM if len(blockers) == 1 else C.LOW
    else:
        action = C.CALL_NOW if cs == VERIFIED_PHONE else C.EMAIL
        if prior_outcome and prior_outcome.get("outcome") == "FOLLOW_UP":
            action = C.FOLLOW_UP
        confidence = C.MEDIUM if any(f in _CONFIDENCE_CAPS for f in flags) else C.HIGH
    return {
        "action": action,
        "confidence": confidence,
        "blockers": [{"gate": g, "action": a, "reason": why} for g, a, why in blockers],
        "flags": sorted(set(flags)),
        "gates": gates,
    }
