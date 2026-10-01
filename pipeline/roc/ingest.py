"""Load official ROC posting-list CSV into internal tables.

Does not write opportunity_score, customer_relevance_score, or
account_priority_score. Does not enable ranking consumption.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pipeline.config.settings import (
    ROC_CLASS_MAP_VERSION,
    ROC_MODEL_VERSION,
    ROC_SOURCE_PAGE,
)
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.db.database import now_iso
from pipeline.roc.classifications import seed_classification_rows
from pipeline.roc.parse import iter_roc_rows, posting_list_metadata

_BATCH = 500


def ingest_posting_list(
    conn: sqlite3.Connection,
    csv_path: Path,
    *,
    source_url: str | None = None,
) -> dict:
    seed_enrichment_registry(conn)
    enabled = conn.execute(
        "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='roc'"
    ).fetchone()
    if enabled is not None and int(enabled[0] or 0) != 0:
        conn.execute(
            "UPDATE enrichment_source_registry SET is_enabled=0, updated_at=? "
            "WHERE source_family='roc'",
            (now_iso(),),
        )

    path = Path(csv_path)
    meta = posting_list_metadata(path)
    retrieved = now_iso()
    source_url = source_url or ROC_SOURCE_PAGE

    before = _checksums(conn)

    conn.execute("DELETE FROM roc_duplicate_candidates")
    conn.execute("DELETE FROM roc_contact_candidates")
    conn.execute("DELETE FROM roc_identity_validations")
    conn.execute("DELETE FROM roc_company_matches")
    conn.execute("DELETE FROM roc_licenses")
    conn.execute("DELETE FROM roc_classification_map")
    conn.execute(
        "DELETE FROM company_enrichment WHERE source_family='roc'"
    )

    conn.executemany(
        """
        INSERT INTO roc_classification_map (
            normalized_class, official_title, corridor_capability,
            secondary_capabilities, mapping_confidence, mapping_version,
            source_url, notes
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        seed_classification_rows(),
    )

    cur = conn.execute(
        """
        INSERT INTO roc_import_runs (
            source_family, source_url, source_filename, source_file_created,
            retrieved_at, row_count, model_version, notes
        ) VALUES ('roc', ?, ?, ?, ?, 0, ?, ?)
        """,
        (
            source_url,
            meta["source_filename"],
            meta["source_file_created"],
            retrieved,
            ROC_MODEL_VERSION,
            json.dumps({"class_map": ROC_CLASS_MAP_VERSION, "stated_records": meta["stated_records"]}),
        ),
    )
    run_id = cur.lastrowid

    rows = []
    n = 0
    for parsed in iter_roc_rows(path):
        rows.append(
            (
                run_id,
                parsed["source_record_key"],
                source_url,
                retrieved,
                parsed["raw_license_number"],
                parsed["normalized_license_number"],
                parsed["raw_business_name"],
                parsed["normalized_business_name"],
                parsed["raw_dba"] or None,
                parsed["normalized_dba"],
                parsed["raw_class"],
                parsed["normalized_class"],
                parsed["raw_class_detail"] or None,
                parsed["raw_class_type"] or None,
                parsed["raw_status"] or None,
                parsed["normalized_status"],
                parsed["issued_date"],
                parsed["expiration_date"],
                parsed["qualifying_party"],
                parsed["normalized_qualifying_party"],
                parsed["address_line_1"],
                parsed["city"],
                parsed["state"],
                parsed["postal_code"],
                parsed["phone"],
                parsed["email"],
                parsed["corridor_capability"],
                parsed["secondary_capabilities"],
                parsed["mapping_confidence"],
                parsed["mapping_version"],
                parsed["raw_json"],
                retrieved,
                retrieved,
            )
        )
        n += 1
        if len(rows) >= _BATCH:
            _insert_licenses(conn, rows)
            rows = []
    if rows:
        _insert_licenses(conn, rows)

    conn.execute("UPDATE roc_import_runs SET row_count=? WHERE id=?", (n, run_id))
    conn.commit()
    after = _checksums(conn)
    if before != after:
        raise RuntimeError("ROC ingest mutated ranking/score tables")
    roc_enabled = conn.execute(
        "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='roc'"
    ).fetchone()
    return {
        "import_run_id": run_id,
        "rows": n,
        "source_filename": meta["source_filename"],
        "source_file_created": meta["source_file_created"],
        "source_url": source_url,
        "roc_enabled": int(roc_enabled[0] if roc_enabled else 0),
        "model_version": ROC_MODEL_VERSION,
        "score_checksum_unchanged": True,
    }


def _insert_licenses(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO roc_licenses (
            import_run_id, source_record_key, source_url, retrieved_at,
            raw_license_number, normalized_license_number,
            raw_business_name, normalized_business_name,
            raw_dba, normalized_dba, raw_class, normalized_class,
            raw_class_detail, raw_class_type, raw_status, normalized_status,
            issued_date, expiration_date, qualifying_party,
            normalized_qualifying_party, address_line_1, city, state,
            postal_code, phone, email, corridor_capability,
            secondary_capabilities, mapping_confidence, mapping_version,
            raw_json, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )


def _checksums(conn: sqlite3.Connection) -> tuple:
    def _sum(sql: str) -> tuple:
        row = conn.execute(sql).fetchone()
        return tuple(row) if row else ()

    return (
        _sum("SELECT COUNT(*), COALESCE(SUM(opportunity_score),0) FROM projects"),
        _sum("SELECT COUNT(*), COALESCE(SUM(relevance_score),0) FROM project_customer_relevance"),
        _sum("SELECT COUNT(*), COALESCE(SUM(account_priority_score),0) FROM company_customer_priority"),
    )
