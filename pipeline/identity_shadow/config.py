"""Identity-shadow configuration and versions.

Every derived row records the parser / normalizer / rules versions that
produced it, so the shadow layer can be rebuilt when any of them change.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field

PARSER_VERSION = "shadow-parser-v1"
NORMALIZER_VERSION = "shadow-normalizer-v2"
RULES_VERSION = "shadow-rules-v2"

# Source families. ``independent`` = counts toward independent corroboration.
# Repeated evidence from one family is never counted twice. Families derived
# from CorridorIQ's own matching (crosswalks, peer inheritance, inferred
# patterns) are evidence but never independent corroboration.
FAMILY_CLASSES = {
    "permit": ("municipal_permit", 1),
    "roc": ("contractor_license_registry", 1),
    "roc_crosswalk": ("corridoriq_derived", 0),
    "contact:prior_research": ("contact_research", 1),
    "contact:official_website": ("company_published", 1),
    "contact:roc": ("contractor_license_registry", 0),   # same origin as ROC
    "contact:canonical_peer": ("corridoriq_derived", 0),
    "contact:inferred_pattern": ("corridoriq_derived", 0),
    "contact:bbb": ("third_party_directory", 1),
    "contact:public_directory": ("third_party_directory", 1),
    "contact:public_utility_list": ("public_record", 1),
    "contact:public_licensing": ("public_record", 1),
    "contact:public_business_record": ("public_record", 1),
}


def family_class(family: str) -> tuple[str, int]:
    """(class, independent) for a family. Permit families are one per
    jurisdiction ("permit:mesa_az"); unknown contact families are treated as
    independent third-party evidence only if listed — otherwise not."""
    if family.startswith("permit:"):
        return FAMILY_CLASSES["permit"]
    return FAMILY_CLASSES.get(family, ("unclassified", 0))


@dataclass
class ShadowConfig:
    as_of: str | None = None            # ISO date for "recent activity"; None = today
    recent_days: int = 90
    shared_value_names: int = 3         # a phone/address/domain asserted by more than N
                                        # distinct business names is non-identifying
    block_cap: int = 40                 # max signatures compared inside one block
    max_component_names: int = 8        # refuse unions that would chain > N distinct names
    include_history: bool = True
    families_limit: tuple = field(default_factory=tuple)  # tests may restrict families

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)
