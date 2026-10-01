"""Internal contractor-intelligence foundation."""

from pipeline.contractor_intel.classify import classify_companies
from pipeline.contractor_intel.taxonomy import (
    ATTRIBUTION_ROLES,
    CAPABILITIES,
    CAPABILITY_CLASSES,
    TRADE_CAPABILITIES,
)

__all__ = [
    "ATTRIBUTION_ROLES",
    "CAPABILITIES",
    "CAPABILITY_CLASSES",
    "TRADE_CAPABILITIES",
    "classify_companies",
]
