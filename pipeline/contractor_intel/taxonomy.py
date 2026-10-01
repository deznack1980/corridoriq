"""Internal contractor capability taxonomy.

Capabilities are multi-label: a company may hold several at once.
``multi_trade`` is derived when two or more trade capabilities fire.
``other`` is a residual for contractor-linked companies with no trade signal.
"""

from __future__ import annotations

CAPABILITIES = (
    "plumbing",
    "fuel_gas",
    "hvac_mechanical",
    "site_utility",
    "general_contractor",
    "electrical",
    "roofing",
    "concrete",
    "framing",
    "drywall",
    "flooring",
    "cabinetry",
    "other",
    "multi_trade",
)

CAPABILITY_SET = frozenset(CAPABILITIES)

# Trades that count toward multi_trade. GC and residual "other" do not.
TRADE_CAPABILITIES = frozenset(
    {
        "plumbing",
        "fuel_gas",
        "hvac_mechanical",
        "site_utility",
        "electrical",
        "roofing",
        "concrete",
        "framing",
        "drywall",
        "flooring",
        "cabinetry",
    }
)

SIGNAL_PERMIT_TYPE = "permit_type"
SIGNAL_PROJECT_CATEGORY = "project_category"
SIGNAL_DESCRIPTION = "description_phrase"
SIGNAL_COMPANY_NAME = "company_name"
SIGNAL_LICENSE = "license_field"
SIGNAL_DERIVED = "derived"

SIGNAL_TYPES = (
    SIGNAL_PERMIT_TYPE,
    SIGNAL_PROJECT_CATEGORY,
    SIGNAL_DESCRIPTION,
    SIGNAL_COMPANY_NAME,
    SIGNAL_LICENSE,
    SIGNAL_DERIVED,
)

# Lifestyle / amenity phrases that often co-occur with gas but do not,
# by themselves, prove the contractor performs fuel-gas work.
LIFESTYLE_GAS_PHRASES = frozenset(
    {
        "outdoor kitchen",
        "bbq",
        "barbecue",
        "barbeque",
        "fire pit",
        "firepit",
        "pool heater",
    }
)

# Generic fixture mentions common on new-home GC permits. Weak unless a
# trade contractor is doing the work on a trade permit.
FIXTURE_GAS_PHRASES = frozenset(
    {
        "gas range",
        "gas dryer",
        "gas cooktop",
        "gas fireplace",
        "gas appliance",
    }
)

# Phrases that actually indicate fuel-gas contracting.
STRONG_GAS_PHRASES = frozenset(
    {
        "fuel gas",
        "natural gas",
        "gas line",
        "gas piping",
        "gas pipe",
        "gas service",
        "gas meter",
        "propane",
        "lp gas",
        "lpg",
        "csst",
        "tracpipe",
        "pex gas",
        "gas pex",
        "approved gas piping",
        "gas appliance connection",
        "generator gas connection",
        "gas water heater",
        "gas furnace",
        "generator gas",
        "gas generator",
        "gas pool heater",
        "gas distribution",
    }
)

# Subset that indicates the company is doing piping/service work, not
# merely mentioning a gas appliance on a pool or new-home permit.
GAS_PIPING_PHRASES = frozenset(
    {
        "fuel gas",
        "gas line",
        "gas piping",
        "gas pipe",
        "gas service",
        "gas meter",
        "propane",
        "lp gas",
        "lpg",
        "csst",
        "tracpipe",
        "pex gas",
        "gas pex",
        "approved gas piping",
        "gas appliance connection",
        "generator gas connection",
        "gas distribution",
    }
)

GENERIC_GAS_PHRASE = "gas"

CORE_PLUMBING_PHRASES = frozenset(
    {
        "plumbing",
        "plumber",
        "repipe",
        "re pipe",
        "water heater",
        "water heater replacement",
        "water line",
        "water service",
        "sewer",
        "septic",
        "backflow",
        "drain line",
        "drain cleaning",
        "potable",
    }
)

ATTRIBUTION_ROLES = (
    "trade_contractor",
    "gc_of_record",
    "subcontractor_if_known",
    "owner_builder",
    "developer",
    "architect_engineer_if_present",
    "unknown_role",
)
ATTRIBUTION_ROLE_SET = frozenset(ATTRIBUTION_ROLES)

NON_TRADE_ROLES = frozenset(
    {
        "gc_of_record",
        "owner_builder",
        "developer",
        "architect_engineer_if_present",
    }
)

CAPABILITY_CLASSES = (
    "specialist_capability",
    "recurring_trade_capability",
    "incidental_project_scope",
    "insufficient_evidence",
)
CAPABILITY_CLASS_SET = frozenset(CAPABILITY_CLASSES)

EVIDENCE_DIRECT = "direct"
EVIDENCE_INDIRECT = "indirect"
EVIDENCE_DIRECTNESS = (EVIDENCE_DIRECT, EVIDENCE_INDIRECT)

# Production / national builders seen in this corridor. Name match is
# case-insensitive substring on the normalized company name.
PRODUCTION_BUILDER_MARKERS = (
    "lennar",
    "shea homes",
    "higley homes",
    "landsea homes",
    "blandford homes",
    "ashton woods",
    "woodside homes",
    "meritage",
    "pulte",
    "kb home",
    "dr horton",
    "d r horton",
    "toll brothers",
    "taylor morrison",
    "richmond american",
    "century communities",
    "fulton homes",
    "maracay",
    "lgi homes",
    "mattamy",
    "camelot homes",
    "elliott homes",
    "american west",
    "gehan",
    "david weekley",
    "tri pointe",
    "william lyon",
    "avanti homes",
    "thrive home",
)

# GC incidental trade mentions stay below medium (50) and never "high".
GC_INCIDENTAL_CAP = 36.0
POOL_PLUMBING_CAP = 45.0
LANDSCAPE_PLUMBING_CAP = 45.0
LIFESTYLE_GAS_CAP = 42.0
NAME_ONLY_CAP = 26.0
GENERIC_GAS_CAP = 0.0
# Phase 2 published baseline — used for BEFORE vs AFTER reports.
PHASE2_BASELINE = {
    "model_version": "contractor-intel-v1",
    "fuel_gas": {"total": 810, "high": 295, "medium": 326, "borderline": 189},
    "plumbing": {"total": 1980, "high": 646, "medium": 568, "borderline": 766},
    "both": 293,
    "useful_pct": 79.1,
    "useful_projects": 59574,
    "linked_projects": 75299,
    "evidence_rows": 64408,
    "runtime_s": 33.3,
    "high_gc_fuel_gas_examples": (
        "SHEA HOMES",
        "HIGLEY HOMES, LLC",
        "LENNAR ARIZONA CONS. CO",
    ),
}
