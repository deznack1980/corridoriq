"""Customer-type profiles for shadow relevance scoring.

Only ``plumbing_supply`` is enabled. Other profiles are reserved so the
same project can later score differently for an electrical distributor
or roofing supplier without changing opportunity_score.
"""

from __future__ import annotations

PROFILE_PLUMBING_SUPPLY = "plumbing_supply"
PROFILE_ELECTRICAL = "electrical_distributor"
PROFILE_ROOFING = "roofing_supplier"
PROFILE_GENERAL = "general_construction"

PROFILES = (
    (
        PROFILE_PLUMBING_SUPPLY,
        "Plumbing / fuel-gas material supply (internal)",
        1,
        "Enabled for shadow scoring. Not used by dashboard ranking yet.",
    ),
    (
        PROFILE_ELECTRICAL,
        "Electrical distributor (reserved)",
        0,
        "Reserved. Do not score in this phase.",
    ),
    (
        PROFILE_ROOFING,
        "Roofing supplier (reserved)",
        0,
        "Reserved. Do not score in this phase.",
    ),
    (
        PROFILE_GENERAL,
        "General construction supplier (reserved)",
        0,
        "Reserved. Do not score in this phase.",
    ),
)
