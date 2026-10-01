"""Book-match configuration. Every threshold is explicit and recorded with each
match run, so a classification can always be traced to the policy that made it.

Defaults are conservative. "Former customer" is OFF unless the supplier
defines its own threshold — elapsed time alone does not make someone a former
customer.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date

RULES_VERSION = "book-match-v1"

# Sales lanes that make a company relevant to a plumbing / wet-side supplier
# for the net-new report. Read from the shared company_sales_lanes table.
DEFAULT_RELEVANT_LANES = (
    "PLUMBING_CORE",
    "FUEL_GAS_PROPANE",
    "FIRE_BACKFLOW",
    "CIVIL_WET_UTILITY",
    "HVAC_MECHANICAL",
)


@dataclass
class BookMatchConfig:
    # Purchase recency (supplier policy).
    active_purchase_days: int = 90          # last purchase within N days → active customer
    former_after_days: int | None = None    # last purchase older than N days → former (None = never infer)
    # Market activity (CorridorIQ permit activity per matched company).
    market_window_days: int = 90            # recent window for permit activity
    market_min_permits: int = 1             # permits in the recent window to count as an active market
    # Matching policy.
    name_review_min_similarity: float = 0.75  # token overlap needed for a name-only REVIEW candidate
    allow_name_city_high_confidence: bool = False  # exact name + same city alone never HIGH by default
    max_candidates: int = 5
    # Net-new.
    relevant_lanes: tuple = DEFAULT_RELEVANT_LANES
    net_new_limit: int = 250
    # Evaluation date (None = today, UTC). Fixed in tests for determinism.
    as_of: str | None = None

    def as_of_date(self) -> date:
        return date.fromisoformat(self.as_of) if self.as_of else date.today()

    def validate(self) -> "BookMatchConfig":
        if self.active_purchase_days <= 0:
            raise ValueError("active_purchase_days must be positive")
        if self.former_after_days is not None and self.former_after_days <= self.active_purchase_days:
            raise ValueError("former_after_days must exceed active_purchase_days")
        if self.market_window_days <= 0 or self.market_min_permits <= 0:
            raise ValueError("market window and minimum permits must be positive")
        if not 0.5 <= self.name_review_min_similarity <= 1.0:
            raise ValueError("name_review_min_similarity must be between 0.5 and 1.0")
        if self.as_of:
            date.fromisoformat(self.as_of)
        self.relevant_lanes = tuple(self.relevant_lanes)
        return self

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_file(cls, path) -> "BookMatchConfig":
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        known = set(cls.__dataclass_fields__)
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        return cls(**data).validate()
