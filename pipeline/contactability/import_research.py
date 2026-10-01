"""Import prior CorridorIQ research CSVs into company_contact_channels."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from pipeline.config.settings import CONTACT_RESEARCH_DIR, CONTACT_RESEARCH_DOWNLOADS
from pipeline.contactability.normalize import (
    INFERRED_UNVERIFIED,
    research_status_to_verification,
)
from pipeline.contactability.store import canonical_id_for, mark_primaries, upsert_channel

_STATUS_RANK = {"Found": 3, "Partial": 2, "Ambiguous": 1}


def research_csv_paths() -> list[Path]:
    paths: list[Path] = []
    for root in (CONTACT_RESEARCH_DOWNLOADS, CONTACT_RESEARCH_DIR):
        if root is None or not root.exists():
            continue
        paths.extend(
            p
            for p in root.glob("corridoriq_contact_enrichment*.csv")
            if "rejected" not in p.name.lower()
        )
    # Stable unique by name, newest path wins if duplicated.
    by_name: dict[str, Path] = {}
    for p in sorted(paths):
        by_name[p.name] = p
    return list(by_name.values())


def _best_rows_by_company(paths: list[Path]) -> dict[str, dict]:
    best: dict[str, dict] = {}
    for path in paths:
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                cid = str(row.get("company_id") or "").strip()
                if not cid:
                    continue
                row["_source_file"] = path.name
                rank = _STATUS_RANK.get(row.get("research_status") or "", 0)
                prev = best.get(cid)
                prev_rank = 0 if prev is None else _STATUS_RANK.get(prev.get("research_status") or "", 0)
                if prev is None or rank > prev_rank:
                    best[cid] = row
    return best


def _inferred_email(email: str | None, notes: str | None) -> bool:
    blob = f"{email or ''} {notes or ''}".lower()
    return "infer" in blob or "pattern" in blob or "guess" in blob


def import_research_csvs(conn: sqlite3.Connection, paths: list[Path] | None = None) -> dict:
    files = paths if paths is not None else research_csv_paths()
    best = _best_rows_by_company(files)
    written = 0
    skipped_missing = 0
    companies_touched: set[int] = set()
    for cid_s, row in best.items():
        try:
            cid = int(cid_s)
        except ValueError:
            continue
        exists = conn.execute("SELECT id FROM companies WHERE id=?", (cid,)).fetchone()
        if exists is None:
            skipped_missing += 1
            continue
        can = canonical_id_for(conn, cid)
        status = research_status_to_verification(
            row.get("research_status"), row.get("research_confidence")
        )
        src = f"prior_research:{row.get('_source_file')}"
        conf = 85.0 if status == "VERIFIED" else 55.0
        fields = [
            ("business_phone", row.get("research_phone") or row.get("primary_contact_phone")),
            ("business_email", row.get("research_email")),
            ("website", row.get("research_website")),
            ("business_address", row.get("research_address")),
        ]
        for ctype, value in fields:
            if not (value or "").strip():
                continue
            vstatus = status
            notes = None
            if ctype == "business_email" and _inferred_email(value, row.get("other_useful_information")):
                vstatus = INFERRED_UNVERIFIED
                notes = "Email labeled INFERRED_UNVERIFIED; not treated as verified."
            if upsert_channel(
                conn,
                company_id=cid,
                contact_type=ctype,
                contact_value=value,
                source_family="prior_research",
                source_reference=src,
                contact_name=row.get("primary_contact_name"),
                title=row.get("primary_contact_title") if ctype != "website" else None,
                verification_status=vstatus,
                confidence=conf,
                notes=notes,
                canonical_company_id=can,
                verified_at=row.get("researched_at") if vstatus == "VERIFIED" else None,
            ):
                written += 1
                companies_touched.add(cid)
        name = (row.get("primary_contact_name") or "").strip()
        title = (row.get("primary_contact_title") or "").strip()
        if name:
            named_type = "qualifying_party" if "qualifying" in title.lower() else "named_contact"
            if upsert_channel(
                conn,
                company_id=cid,
                contact_type=named_type,
                contact_value=name,
                source_family="prior_research",
                source_reference=src,
                contact_name=name,
                title=title or None,
                verification_status=status,
                confidence=conf,
                canonical_company_id=can,
                verified_at=row.get("researched_at") if status == "VERIFIED" else None,
            ):
                written += 1
                companies_touched.add(cid)
            if (row.get("primary_contact_email") or "").strip():
                if upsert_channel(
                    conn,
                    company_id=cid,
                    contact_type="business_email",
                    contact_value=row["primary_contact_email"],
                    source_family="prior_research",
                    source_reference=src,
                    contact_name=name,
                    title=title or None,
                    verification_status=status,
                    confidence=conf,
                    canonical_company_id=can,
                ):
                    written += 1
        mark_primaries(conn, cid)
    conn.commit()
    return {
        "files": [str(p) for p in files],
        "unique_research_companies": len(best),
        "channels_written": written,
        "companies_touched": len(companies_touched),
        "skipped_missing_company": skipped_missing,
    }
