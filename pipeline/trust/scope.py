"""Per-permit trade scope from the permit's own text. No account history.

The stored why-now labels a permit with the account's historical primary
demand category, and `primary_category` prefers plumbing_service over
fuel_gas when both appear. This module reads one permit and names what that
permit says, with the matched phrases kept as evidence. A gas line is fuel
gas. A fire line is a fire line. Neither is described as plumbing service.
"""

from __future__ import annotations

import re

PLUMBING_WATER = "PLUMBING_WATER"
WATER_HEATER = "WATER_HEATER"
FUEL_GAS = "FUEL_GAS"
PROPANE = "PROPANE"
FIRE_LINE = "FIRE_LINE"
BACKFLOW = "BACKFLOW"
CIVIL_WET = "CIVIL_WET"
MECHANICAL = "MECHANICAL"
UNKNOWN_WET = "UNKNOWN_WET"
ELECTRICAL = "ELECTRICAL"
OTHER = "OTHER"

# Most specific first. Used to pick the display label when a permit has several.
PRECEDENCE = (
    FIRE_LINE,
    PROPANE,
    WATER_HEATER,
    FUEL_GAS,
    PLUMBING_WATER,
    BACKFLOW,
    CIVIL_WET,
    MECHANICAL,
    UNKNOWN_WET,
    ELECTRICAL,
    OTHER,
)

WET_SCOPES = frozenset(
    {PLUMBING_WATER, WATER_HEATER, FUEL_GAS, PROPANE, FIRE_LINE, BACKFLOW, CIVIL_WET, MECHANICAL, UNKNOWN_WET}
)

LABELS = {
    PLUMBING_WATER: "plumbing (domestic water / drain / fixture)",
    WATER_HEATER: "water heater",
    FUEL_GAS: "fuel-gas piping",
    PROPANE: "propane / LP gas",
    FIRE_LINE: "fire line / hydrant / sprinkler",
    BACKFLOW: "backflow",
    CIVIL_WET: "civil wet utility",
    MECHANICAL: "HVAC / mechanical",
    UNKNOWN_WET: "plumbing permit, scope not stated",
    ELECTRICAL: "electrical",
    OTHER: "no wet-side scope",
}

# Lane keys match pipeline.sales_lanes.lanes. Kept as strings so the operator
# does not import lane assignment code.
LANE_FOR_SCOPE = {
    PLUMBING_WATER: "PLUMBING_CORE",
    WATER_HEATER: "PLUMBING_CORE",
    UNKNOWN_WET: "PLUMBING_CORE",
    FUEL_GAS: "FUEL_GAS_PROPANE",
    PROPANE: "FUEL_GAS_PROPANE",
    FIRE_LINE: "FIRE_BACKFLOW",
    BACKFLOW: "FIRE_BACKFLOW",
    CIVIL_WET: "CIVIL_WET_UTILITY",
    MECHANICAL: "HVAC_MECHANICAL",
}

# Product A lanes a supply-house rep could act on. Other wet lanes go to review.
CALLABLE_LANES = ("PLUMBING_CORE", "FUEL_GAS_PROPANE")
LANE_RANK = {"PLUMBING_CORE": 0, "FUEL_GAS_PROPANE": 1}

_PATTERNS: dict[str, re.Pattern] = {
    FIRE_LINE: re.compile(
        r"\bfire\s*lines?\b|\bfirelines?\b|\bhydrants?\b|\bfire\s*sprinklers?\b|\bsprinklers?\b"
        r"|\bfire\s*protection\b|\bfire\s*service\b|\bstandpipes?\b|\bfire\s*riser\b"
    ),
    PROPANE: re.compile(
        r"\bpropane\b|\blp\s*gas\b|\blpg\b|\blp\s*(?:tanks?|containers?|systems?)\b"
    ),
    WATER_HEATER: re.compile(
        r"\bwater\s*heaters?\b|\bwtr\s*htr\b|\bw/?h\s*(?:replace|change|swap)|\btankless\b"
    ),
    FUEL_GAS: re.compile(
        r"\bgas\s*(?:lines?|pipe|pipes|piping|meter|service|stub|test|yard\s*line|riser|outlet|valve"
        r"|connection|distribution|system|sys)\b"
        r"|\bfuel\s*gas\b|\bcsst\b|\bnat(?:ural)?\s*gas\b"
        r"|\bgas\b.{0,40}\b(?:meter|mtr|pool\s*h(?:ea)?te?r|bbq|fire\s*pits?|firepits?|fireplace|range|dryer|grill)\b"
        r"|\b(?:meter|mtr)\b.{0,40}\b(?:pool\s*h(?:ea)?te?r|htr|bbq|fire\s*pits?|firepits?|fireplace|generator"
        r"|range|dryer|grill|fire\s*bars?)\b"
    ),
    PLUMBING_WATER: re.compile(
        r"\bre-?pipe|\brepiping\b|\bhot\s*(?:and|&|/)\s*cold\b"
        r"|\bwater\s*(?:lines?|pipes?|piping|service|supply|distribution)\b|\bdomestic\s*water\b"
        r"|\bsupply\s*lines?\b|\bpotable\b|\bpex\b|\bprv\b|\bpressure\s*reduc|\bwater\s*soften"
        r"|\bfixtures?\b|\btoilets?\b|\bwater\s*closets?\b|\blavator|\bsinks?\b|\bshowers?\b"
        r"|\bfaucets?\b|(?<!storm )\bdrains?\b|\bdwv\b|\bwaste\s*(?:lines?|and\s*vent|&\s*vent)"
        r"|\bsewer\b(?!\s*mains?\b)|\bseptic\b|\bcleanouts?\b|\bgrease\s*(?:trap|interceptor)"
        r"|\bsewage\s*ejector|\bslab\s*leak"
    ),
    BACKFLOW: re.compile(r"\bbackflow\b|\brpz\b|\bdouble\s*check\b"),
    CIVIL_WET: re.compile(
        r"\bwater\s*mains?\b|\bsewer\s*mains?\b|\b(?:on-?site|private|offsite|off-site)\s*(?:water|sewer|utilit)"
        r"|\bsite\s*utilit|\bwet\s*utilit|\bstorm\s*drain|\bwaterline\s*extension|\bunderground\s*utilit"
    ),
    MECHANICAL: re.compile(
        r"\bhvac\b|\bmechanical\b|\bfurnace\b|\bheat\s*pumps?\b|\bair\s*handlers?\b|\bcondens(?:er|ing\s*unit)"
        r"|\bductwork\b|\bmini[-\s]?split|\ba/c\b|\bevap(?:orative)?\s*cooler|\bpackage\s*units?\b"
        r"|\brooftop\s*units?\b|\brtu\b|\bboilers?\b|\bchillers?\b|\bhydronic\b"
    ),
    ELECTRICAL: re.compile(
        r"\belectric|\bpanel\b|\bmpu\b|\bses\b|\bservice\s*entrance|\bev\s*charg|\bsolar\b|\bpv\b"
        r"|\bphotovoltaic|\bbattery\b|\bess\b|\bcircuits?\b|\bwiring\b|\bmain\s*breaker|\bmeter\s*(?:base|socket)"
        r"|\b\d{2,3}\s*a(?:mp)?\b"
    ),
}

_GENERIC_PIPE = {"repipe", "repiping"}
# Work that often includes plumbing even when the permit does not list trades.
_GENERAL_BUILDING = re.compile(
    r"\bnew\s+(?:\d[\d,]*\s*(?:sf|sq\s*ft)\s+)?(?:building|construction|home|residence|house|sfr|structure|restaurant|office|store)"
    r"|\btenant\s*improvement|\bti\b|\bremodel|\brenovat|\balteration|\baddition\b|\bbuild[-\s]?out"
    r"|\bshell\b|\bkitchen\b|\bbath(?:room)?s?\b|\bcasita\b|\bguest\s*house|\badu\b"
)
# Specific non-wet trades. Needed before a project is hidden from a feed.
_NON_WET_TRADE = re.compile(
    r"\bracking\b|\bracks?\b|\bshelving\b|\bmezzanine\b|\bsigns?\b|\bsignage\b|\bmonument\b|\bbanner\b"
    r"|\broof(?:ing)?\b|\bre-?roof|\bfenc(?:e|ing)\b|\bdemolition\b|\bdemo\b|\bstucco\b|\bpaint(?:ing)?\b"
    r"|\bwindows?\b|\bdoors?\b|\bgarage\s*door|\bshade\s*(?:structure|canopy)|\bcarport\b|\bpatio\s*cover"
    r"|\bretaining\s*wall|\bdriveway\b|\bparking\s*lot|\bstriping\b|\bhigh[-\s]*pile|\bcombustible\s*storage"
    r"|\btent\b|\bfireworks?\b|\bspecial\s*event|\btemporary\s*(?:structure|power)|\bantenna|\bcell\s*tower"
)
_POOL_CONTEXT = re.compile(r"\bpool\b|\bspa\b")
_PLUMB_TYPE = re.compile(r"\bplumb|\bplmb\b")
_GAS_TYPE = re.compile(r"\bgas\b")
_FIRE_TYPE = re.compile(r"\bfire\b")
_MECH_TYPE = re.compile(r"\bmechanical\b|\bhvac\b|\bmech\b")
_ELEC_TYPE = re.compile(r"\belectric|\belec\b")
_WS = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    if not text:
        return ""
    t = str(text).lower().replace("_", " ").replace("—", " ").replace("–", " ")
    return _WS.sub(" ", t).strip()


def classify_permit(
    *,
    permit_type: str | None,
    permit_subtype: str | None = None,
    description: str | None,
    project_description: str | None = None,
) -> dict:
    """Return the scope classes a single permit's own fields support."""
    blob = normalize(" ".join(p for p in (description, project_description) if p))
    ptype = normalize(" ".join(p for p in (permit_type, permit_subtype) if p))
    matched: dict[str, list[str]] = {}
    for scope, pattern in _PATTERNS.items():
        hits = sorted({m.group(0).strip()[:40] for m in pattern.finditer(blob)})
        if hits:
            matched[scope] = hits[:4]
    # "Repipe existing gas system" is gas work. Generic piping words alone do
    # not make a gas permit a plumbing permit.
    if FUEL_GAS in matched and PLUMBING_WATER in matched:
        if {normalize(t).replace("-", "") for t in matched[PLUMBING_WATER]} <= _GENERIC_PIPE:
            del matched[PLUMBING_WATER]
    basis = {scope: "description" for scope in matched}

    # Permit type is an observed field too, but weaker than stated scope.
    type_scopes = []
    if _GAS_TYPE.search(ptype):
        type_scopes.append(FUEL_GAS)
    if _FIRE_TYPE.search(ptype):
        type_scopes.append(FIRE_LINE)
    if _MECH_TYPE.search(ptype):
        type_scopes.append(MECHANICAL)
    if _ELEC_TYPE.search(ptype):
        type_scopes.append(ELECTRICAL)
    for scope in type_scopes:
        if scope not in matched:
            matched[scope] = [f"permit type: {ptype}"]
            basis[scope] = "permit_type"
    plumbing_type = bool(_PLUMB_TYPE.search(ptype))
    wet_found = any(scope in WET_SCOPES for scope in matched)
    if plumbing_type and not wet_found:
        matched[UNKNOWN_WET] = [f"permit type: {ptype}"]
        basis[UNKNOWN_WET] = "permit_type"

    scopes = [scope for scope in PRECEDENCE if scope in matched]
    if not scopes:
        scopes = [OTHER]
        basis[OTHER] = "none"
    wet = [scope for scope in scopes if scope in WET_SCOPES]
    primary = wet[0] if wet else scopes[0]
    lanes = []
    for scope in wet:
        lane = LANE_FOR_SCOPE[scope]
        if lane not in lanes:
            lanes.append(lane)
    return {
        "scopes": scopes,
        "wet_scopes": wet,
        "primary": primary,
        "primary_label": LABELS[primary],
        "labels": [LABELS[s] for s in wet] or [LABELS[primary]],
        "lanes": lanes,
        "matched_terms": matched,
        "basis": basis,
        "explicit": basis.get(primary) == "description",
        "pool_context": bool(_POOL_CONTEXT.search(blob)),
        "plumbing_permit_type": plumbing_type,
    }


WET_STATED = "WET_SCOPE"
NOT_WET_STATED = "NOT_WET_SCOPE"
SCOPE_NOT_STATED = "SCOPE_NOT_STATED"


def project_trade_scope(
    *,
    permit_type: str | None,
    permit_subtype: str | None = None,
    description: str | None,
    project_description: str | None = None,
) -> dict:
    """Three states, not two. Hiding a project needs positive evidence.

    WET_SCOPE: the permit's own text or type states wet-side work.
    NOT_WET_SCOPE: the permit names a specific non-wet trade (racking, signage,
        solar, electrical, roofing...) and no general building work.
    SCOPE_NOT_STATED: blank, unclear, or general building work (new building,
        tenant improvement, remodel) whose trades are not listed. These may
        well include plumbing, so they are never hidden.
    """
    sc = classify_permit(
        permit_type=permit_type,
        permit_subtype=permit_subtype,
        description=description,
        project_description=project_description,
    )
    text = normalize(" ".join(p for p in (description, project_description) if p))
    if sc["wet_scopes"]:
        state, label = WET_STATED, " + ".join(sc["labels"])
    elif _GENERAL_BUILDING.search(text):
        state, label = SCOPE_NOT_STATED, "Building work; trades not stated"
    elif ELECTRICAL in sc["scopes"] or _NON_WET_TRADE.search(text):
        state, label = NOT_WET_STATED, "No plumbing or wet-side scope stated"
    else:
        state, label = SCOPE_NOT_STATED, "Scope not stated on permit" if not text else "Scope unclear"
    return {"state": state, "label": label, "lane": primary_lane(sc), "terms": sc["matched_terms"]}


def primary_lane(scope: dict, prefer: tuple[str, ...] = CALLABLE_LANES) -> str | None:
    lanes = scope.get("lanes") or []
    for lane in prefer:
        if lane in lanes:
            return lane
    return lanes[0] if lanes else None


def scope_for_lane(scope: dict, lane: str | None) -> str | None:
    """The most specific wet scope on the permit that belongs to lane."""
    for s in scope.get("wet_scopes") or []:
        if LANE_FOR_SCOPE.get(s) == lane:
            return s
    return None
