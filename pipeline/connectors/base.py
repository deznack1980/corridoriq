"""Base connector interface.

Every jurisdiction connector maps real source records into the normalized
`permits` column shape. The one rule that must never be violated: a field
the source doesn't provide is set to None. Never guess, never invent.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional


class ConnectorNotConfiguredError(Exception):
    """Raised when a connector is registered but lacks a verified endpoint."""


@dataclass
class ConnectorResult:
    jurisdiction_slug: str
    records: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    # Rows the source actually returned, before exact duplicates were dropped.
    source_row_count: int = 0
    duplicates_dropped: int = 0

    @property
    def fetched_count(self) -> int:
        return len(self.records)


# The full set of normalized `permits` columns every connector maps into.
PERMIT_FIELDS = [
    "permit_number", "permit_type", "permit_subtype", "status", "description",
    "filed_date", "issued_date", "expiration_date", "finaled_date",
    "inspection_status", "last_inspection_date", "job_address", "city", "state",
    "zip", "parcel_number", "apn", "latitude", "longitude", "owner_name",
    "general_contractor_name", "plumbing_contractor_name",
    "contractor_license_number", "valuation", "square_footage",
    "occupancy_type", "permit_url", "public_notes", "inspector",
    "project_description",
]


class BaseConnector(ABC):
    jurisdiction_slug: str

    def __init__(self, jurisdiction_slug: str):
        self.jurisdiction_slug = jurisdiction_slug

    @abstractmethod
    def fetch_raw(self, since: Optional[datetime] = None) -> Iterable[dict]:
        """Yield raw source records, paginated internally."""
        raise NotImplementedError

    @abstractmethod
    def map_record(self, raw: dict) -> dict:
        """Map one raw record to the normalized `permits` column dict.

        Every key in PERMIT_FIELDS must be present in the result. Any field
        the source doesn't provide MUST be None — never invented or guessed.
        """
        raise NotImplementedError

    def _empty_permit_dict(self) -> dict:
        return {k: None for k in PERMIT_FIELDS}

    def run(self, since: Optional[datetime] = None) -> ConnectorResult:
        """Fetch and map, dropping rows that are exact duplicates.

        Some published layers contain the same record more than once. Chandler's
        DSActiveProjects layer returns 170 rows for 85 permits: every permit
        appears exactly twice, identical in all attributes and geometry, and
        differing only in OBJECTID (an Esri internal row id).

        Only EXACT duplicates are dropped. Two rows sharing a permit number but
        disagreeing on any mapped field are both kept - that is a real source
        conflict and must stay visible rather than being quietly resolved here.
        """
        result = ConnectorResult(jurisdiction_slug=self.jurisdiction_slug)
        seen: dict = {}
        for raw in self.fetch_raw(since=since):
            result.source_row_count += 1
            try:
                mapped = self.map_record(raw)
                mapped["jurisdiction"] = self.jurisdiction_slug
                mapped["raw_source_json"] = _safe_json(raw)
            except Exception as exc:  # noqa: BLE001 - one bad record must not kill the run
                result.errors.append(f"{type(exc).__name__}: {exc}")
                continue

            key = mapped.get("permit_number")
            if key is not None:
                fingerprint = _fingerprint(mapped)
                if seen.get(key) == fingerprint:
                    result.duplicates_dropped += 1
                    continue
                seen[key] = fingerprint
            result.records.append(mapped)
        return result


def _fingerprint(mapped: dict) -> tuple:
    """Identity of a mapped record across the fields we actually store.

    Excludes raw_source_json, because the source-internal row id it contains
    (OBJECTID) is exactly what makes duplicate rows look distinct.
    """
    return tuple(mapped.get(f) for f in PERMIT_FIELDS)


def _safe_json(raw: dict) -> str:
    import json

    try:
        return json.dumps(raw, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
