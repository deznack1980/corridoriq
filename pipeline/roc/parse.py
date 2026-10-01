"""Parse Arizona ROC posting-list CSV files."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Iterator

from pipeline.roc.classifications import map_classification
from pipeline.roc.normalize import (
    normalize_city,
    normalize_class_code,
    normalize_company_name,
    normalize_person_name,
    normalize_roc_license,
    normalize_status,
)

_TITLE_DATE = re.compile(r"File created:\s*([A-Za-z]+ \d{1,2}, \d{4})", re.I)
_TITLE_COUNT = re.compile(r"(\d[\d,]*)\s+Records", re.I)


def posting_list_metadata(path: Path) -> dict:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        first = fh.readline()
    date_m = _TITLE_DATE.search(first)
    count_m = _TITLE_COUNT.search(first)
    return {
        "source_file_created": date_m.group(1) if date_m else None,
        "stated_records": int(count_m.group(1).replace(",", "")) if count_m else 0,
        "source_filename": path.name,
    }


def iter_roc_rows(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = None
        for row in reader:
            if not row or not any(cell.strip() for cell in row):
                continue
            blob = ",".join(row).lower()
            if header is None:
                if "license no" in blob or "license_no" in blob:
                    header = [h.strip() for h in row]
                continue
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))
            raw = {header[i]: row[i].strip() for i in range(len(header))}
            parsed = parse_row(raw)
            if parsed:
                yield parsed


def parse_row(raw: dict) -> dict | None:
    license_no = normalize_roc_license(raw.get("License No") or raw.get("license_no"))
    if not license_no:
        return None
    raw_class = raw.get("Class") or raw.get("class")
    detail = raw.get("Class Detail") or raw.get("class_detail")
    mapped = map_classification(raw_class, detail)
    dba = raw.get("Doing Business As") or raw.get("dba") or ""
    qp = raw.get("Qualifying Party") or raw.get("qualifying_party") or ""
    return {
        "source_record_key": f"{license_no}:{mapped['normalized_class'] or '_'}",
        "raw_license_number": (raw.get("License No") or raw.get("license_no") or "").strip(),
        "normalized_license_number": license_no,
        "raw_business_name": (raw.get("Business Name") or raw.get("business_name") or "").strip(),
        "normalized_business_name": normalize_company_name(
            raw.get("Business Name") or raw.get("business_name")
        ),
        "raw_dba": dba.strip(),
        "normalized_dba": normalize_company_name(dba) or None,
        "raw_class": (raw_class or "").strip(),
        "normalized_class": mapped["normalized_class"] or "",
        "raw_class_detail": (detail or "").strip(),
        "raw_class_type": (raw.get("Class Type") or raw.get("class_type") or "").strip(),
        "raw_status": (raw.get("Status") or raw.get("status") or "").strip(),
        "normalized_status": normalize_status(raw.get("Status") or raw.get("status")),
        "issued_date": (raw.get("Issued Date") or raw.get("issued_date") or "").strip() or None,
        "expiration_date": (raw.get("Expiration Date") or raw.get("expiration_date") or "").strip() or None,
        "qualifying_party": qp.strip() or None,
        "normalized_qualifying_party": normalize_person_name(qp) or None,
        "address_line_1": (raw.get("Address") or raw.get("address") or "").strip() or None,
        "city": normalize_city(raw.get("City") or raw.get("city")),
        "state": ((raw.get("State") or raw.get("state") or "").strip().upper() or None),
        "postal_code": (raw.get("Zip") or raw.get("zip") or "").strip() or None,
        "phone": (raw.get("Phone") or raw.get("phone") or "").strip() or None,
        "email": (raw.get("Email") or raw.get("email") or "").strip() or None,
        "corridor_capability": mapped["corridor_capability"],
        "secondary_capabilities": json.dumps(mapped["secondary_capabilities"]),
        "mapping_confidence": mapped["mapping_confidence"],
        "mapping_version": mapped["mapping_version"],
        "raw_json": json.dumps(raw, ensure_ascii=False),
        "normalized_class_code": normalize_class_code(raw_class, detail),
    }
