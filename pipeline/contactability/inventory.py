"""Audit contacts CorridorIQ already possesses. No new collection."""

from __future__ import annotations

import sqlite3

from pipeline.config.settings import CONTACT_RESEARCH_DIR, DATA_DIR, PROJECT_ROOT
from pipeline.contactability.import_research import research_csv_paths
from pipeline.contactability.store import canonical_id_for, upsert_channel


def _copy_company_profile(conn: sqlite3.Connection) -> int:
    written = 0
    for row in conn.execute(
        """
        SELECT id, main_phone, main_email, website, address_line_1, city, postal_code
        FROM companies
        WHERE (main_phone IS NOT NULL AND trim(main_phone) <> '')
           OR (main_email IS NOT NULL AND trim(main_email) <> '')
           OR (website IS NOT NULL AND trim(website) <> '')
           OR (address_line_1 IS NOT NULL AND trim(address_line_1) <> '')
        """
    ):
        can = canonical_id_for(conn, int(row["id"]))
        mapping = [
            ("business_phone", row["main_phone"]),
            ("business_email", row["main_email"]),
            ("website", row["website"]),
        ]
        addr = " ".join(
            p for p in (row["address_line_1"], row["city"], row["postal_code"]) if p
        )
        if addr.strip():
            mapping.append(("business_address", addr))
        for ctype, value in mapping:
            if value and upsert_channel(
                conn,
                company_id=int(row["id"]),
                contact_type=ctype,
                contact_value=value,
                source_family="company_profile",
                source_reference="companies table",
                verification_status="CANDIDATE",
                confidence=50.0,
                canonical_company_id=can,
                notes="Copied from companies.*; not independently re-verified.",
            ):
                written += 1
    return written


def _copy_contacts_table(conn: sqlite3.Connection) -> int:
    written = 0
    for row in conn.execute("SELECT * FROM contacts"):
        can = canonical_id_for(conn, int(row["company_id"]))
        mapping = [
            ("business_phone", row["phone"] or row["mobile_phone"]),
            ("business_email", row["email"]),
        ]
        for ctype, value in mapping:
            if value and upsert_channel(
                conn,
                company_id=int(row["company_id"]),
                contact_type=ctype,
                contact_value=value,
                source_family="contacts_table",
                source_reference=row["source"],
                contact_name=row["full_name"],
                title=row["job_title"],
                verification_status="CANDIDATE",
                confidence=60.0,
                canonical_company_id=can,
            ):
                written += 1
        if row["full_name"]:
            if upsert_channel(
                conn,
                company_id=int(row["company_id"]),
                contact_type="named_contact",
                contact_value=row["full_name"],
                source_family="contacts_table",
                source_reference=row["source"],
                contact_name=row["full_name"],
                title=row["job_title"],
                verification_status="CANDIDATE",
                confidence=60.0,
                canonical_company_id=can,
            ):
                written += 1
    return written


def _copy_roc_addresses(conn: sqlite3.Connection) -> dict:
    counts = {
        r["field_name"]: r["n"]
        for r in conn.execute(
            "SELECT field_name, COUNT(*) n FROM roc_contact_candidates GROUP BY field_name"
        )
    }
    focus_ids = {
        int(r["company_id"])
        for r in conn.execute(
            "SELECT company_id FROM company_customer_priority WHERE profile_key='plumbing_supply' "
            "ORDER BY account_priority_score DESC LIMIT 200"
        )
    }
    for r in conn.execute("SELECT company_id_a, company_id_b FROM roc_duplicate_candidates"):
        focus_ids.add(int(r["company_id_a"]))
        focus_ids.add(int(r["company_id_b"]))
    written = 0
    if not focus_ids:
        return {"written": 0, "field_counts": counts, "focus_companies": 0}
    placeholders = ",".join("?" * len(focus_ids))
    for row in conn.execute(
        f"""
        SELECT company_id, field_name, value, confidence
        FROM roc_contact_candidates
        WHERE company_id IN ({placeholders})
          AND value IS NOT NULL AND trim(value) <> ''
          AND field_name IN ('address_line_1','qualifying_party')
        """,
        list(focus_ids),
    ):
        can = canonical_id_for(conn, int(row["company_id"]))
        ctype = "business_address" if row["field_name"] == "address_line_1" else "qualifying_party"
        if upsert_channel(
            conn,
            company_id=int(row["company_id"]),
            contact_type=ctype,
            contact_value=row["value"],
            source_family="roc",
            source_reference=f"roc_contact_candidates:{row['field_name']}",
            verification_status="CANDIDATE",
            confidence=float(row["confidence"] or 50),
            canonical_company_id=can,
            notes="ROC posting list has address/class/status/QP; no phone or email.",
            contact_name=row["value"] if ctype == "qualifying_party" else None,
            title="Qualifying party" if ctype == "qualifying_party" else None,
        ):
            written += 1
    return {"written": written, "field_counts": counts, "focus_companies": len(focus_ids)}


def existing_asset_scan() -> dict:
    csvs = [str(p) for p in research_csv_paths()]
    extra = []
    for root in (PROJECT_ROOT / "data", DATA_DIR, CONTACT_RESEARCH_DIR):
        if root.exists():
            extra.extend(str(p) for p in root.rglob("*.csv") if "contact" in p.name.lower())
    return {
        "research_csvs": csvs,
        "other_contact_csvs": sorted(set(extra)),
    }


def ingest_existing_contacts(conn: sqlite3.Connection) -> dict:
    profile = _copy_company_profile(conn)
    contacts = _copy_contacts_table(conn)
    roc = _copy_roc_addresses(conn)
    crm = conn.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"]
    empty = conn.execute(
        """
        SELECT
          SUM(CASE WHEN main_phone IS NOT NULL AND trim(main_phone)<>'' THEN 1 ELSE 0 END) phones,
          SUM(CASE WHEN main_email IS NOT NULL AND trim(main_email)<>'' THEN 1 ELSE 0 END) emails,
          SUM(CASE WHEN website IS NOT NULL AND trim(website)<>'' THEN 1 ELSE 0 END) websites,
          COUNT(*) companies
        FROM companies
        """
    ).fetchone()
    conn.commit()
    assets = existing_asset_scan()
    return {
        "company_profile_channels": profile,
        "contacts_table_channels": contacts,
        "roc_address_channels": roc if isinstance(roc, int) else roc.get("written", 0),
        "roc_candidate_fields": {} if isinstance(roc, int) else roc.get("field_counts", {}),
        "crm_relationships": int(crm),
        "companies_with_main_phone": int(empty["phones"] or 0),
        "companies_with_main_email": int(empty["emails"] or 0),
        "companies_with_website": int(empty["websites"] or 0),
        "companies_total": int(empty["companies"] or 0),
        **assets,
    }
