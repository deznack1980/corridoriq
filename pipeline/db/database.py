"""SQLite connection helper: applies schema.sql and seeds jurisdictions.yaml.

Idempotent — safe to call on every pipeline run. This is also the migration
mechanism for Phase 1: editing jurisdictions.yaml and re-running the pipeline
syncs the jurisdictions table forward.
"""

import sqlite3
from datetime import datetime, timezone

import yaml

from pipeline.config.settings import (
    DB_PATH,
    JURISDICTIONS_YAML,
    LEGACY_DB_PATH,
    SCHEMA_PATH,
)

# SQLite has no persistent, file-level foreign-key setting: `PRAGMA
# foreign_keys` is per-connection and defaults to OFF. Enforcement therefore
# depends on every connection going through get_connection(), which is why
# production code must not call sqlite3.connect() directly.
_BUSY_TIMEOUT_MS = 10_000


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stranded_legacy_database() -> bool:
    """True when the configured DB is absent but an old in-repo one exists.

    Without this guard, a misconfigured CORRIDORIQ_DB_PATH silently creates an
    empty database and the pipeline looks like it lost 110k permits.
    """
    try:
        return (
            not DB_PATH.exists()
            and LEGACY_DB_PATH.exists()
            and DB_PATH.resolve() != LEGACY_DB_PATH.resolve()
        )
    except OSError:  # pragma: no cover - resolve() on an unmapped drive
        return False


def apply_connection_pragmas(conn: sqlite3.Connection) -> None:
    """Apply the standard runtime pragmas to an open connection."""
    conn.execute("PRAGMA foreign_keys = ON")
    # Wait rather than fail immediately when another connection holds a lock
    # (API server, scheduled refresh, or a backup running concurrently).
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    # WAL lets readers proceed while the pipeline writes. It is persisted in
    # the database file, so this is a no-op after the first call; setting it
    # every time makes the setting self-healing if a file is ever restored
    # from a non-WAL copy. WAL is unsafe on network/synced filesystems, which
    # is a second reason the database lives outside OneDrive.
    conn.execute("PRAGMA journal_mode = WAL")
    # Safe with WAL: a crash can lose the last transaction but cannot corrupt
    # the database. Full fsync per commit is not worth the cost for a pipeline
    # whose input is re-fetchable.
    conn.execute("PRAGMA synchronous = NORMAL")


def get_connection() -> sqlite3.Connection:
    if _stranded_legacy_database():
        raise RuntimeError(
            f"No database at the configured location:\n  {DB_PATH}\n"
            f"but a legacy in-repo database still exists at:\n  {LEGACY_DB_PATH}\n\n"
            "Refusing to create an empty database and silently lose it.\n"
            "Run:  python scripts/migrate_database_location.py\n"
            "or set CORRIDORIQ_DB_PATH to the database you intend to use."
        )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    apply_connection_pragmas(conn)
    return conn


def apply_schema(conn: sqlite3.Connection) -> None:
    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn.executescript(schema_sql)
    conn.commit()


# Columns added after the initial 2.0 schema. ``apply_schema`` creates new
# tables via CREATE TABLE IF NOT EXISTS, but pre-existing tables need explicit
# ALTERs. Each entry: (table, column, column_definition).
_KNOWLEDGE_V21_COLUMNS = [
    ("projects", "mapping_kb_version", "INTEGER"),
    ("status_dictionary", "mapping_confidence", "REAL"),
    ("status_dictionary", "mapping_source", "TEXT"),
    ("status_dictionary", "lifecycle_state", "TEXT NOT NULL DEFAULT 'active'"),
    ("status_dictionary", "reviewed_by", "TEXT"),
    ("status_dictionary", "reviewed_at", "TEXT"),
    ("status_dictionary", "review_notes", "TEXT"),
    ("status_dictionary", "kb_version", "INTEGER"),
    ("permit_code_dictionary", "mapping_confidence", "REAL"),
    ("permit_code_dictionary", "mapping_source", "TEXT"),
    ("permit_code_dictionary", "lifecycle_state", "TEXT NOT NULL DEFAULT 'active'"),
    ("permit_code_dictionary", "reviewed_by", "TEXT"),
    ("permit_code_dictionary", "reviewed_at", "TEXT"),
    ("permit_code_dictionary", "review_notes", "TEXT"),
    ("permit_code_dictionary", "kb_version", "INTEGER"),
    ("keyword_dictionary", "mapping_confidence", "REAL"),
    ("keyword_dictionary", "mapping_source", "TEXT"),
    ("keyword_dictionary", "lifecycle_state", "TEXT NOT NULL DEFAULT 'active'"),
    ("keyword_dictionary", "reviewed_by", "TEXT"),
    ("keyword_dictionary", "reviewed_at", "TEXT"),
    ("keyword_dictionary", "review_notes", "TEXT"),
    ("keyword_dictionary", "kb_version", "INTEGER"),
    ("knowledge_review_queue", "priority_score", "REAL"),
    ("knowledge_review_queue", "priority_tier", "TEXT"),
    ("knowledge_review_queue", "affected_permit_count", "INTEGER"),
    ("knowledge_review_queue", "affected_recent_count", "INTEGER"),
    ("knowledge_review_queue", "affected_active_count", "INTEGER"),
    ("knowledge_review_queue", "affected_avg_score", "REAL"),
    ("knowledge_review_queue", "priority_computed_at", "TEXT"),
    # Sprint 3 — project lifecycle intelligence (projects table).
    ("projects", "project_lifecycle", "TEXT"),
    ("projects", "opportunity_date", "TEXT"),
    ("projects", "opportunity_date_basis", "TEXT"),
    ("projects", "opportunity_timing", "TEXT"),
    # Sprint 4 — company intelligence links (nullable; raw text columns kept).
    ("projects", "contractor_company_id", "INTEGER REFERENCES companies(id)"),
    ("projects", "owner_company_id", "INTEGER REFERENCES companies(id)"),
    ("projects", "developer_company_id", "INTEGER REFERENCES companies(id)"),
    ("projects", "architect_company_id", "INTEGER REFERENCES companies(id)"),
    ("projects", "engineer_company_id", "INTEGER REFERENCES companies(id)"),
    ("permits", "contractor_company_id", "INTEGER REFERENCES companies(id)"),
    # Sprint 4 Phase 10 — future per-tenant ownership (NULL = shared canonical).
    ("quotes", "organization_id", "INTEGER"),
    ("deliveries", "organization_id", "INTEGER"),
    # Sprint 6 — supplier catalog + routing columns on the pre-existing
    # suppliers table (additive; a UNIQUE index on code is created by schema.sql).
    ("suppliers", "code", "TEXT"),
    ("suppliers", "warehouse_address", "TEXT"),
    ("suppliers", "city", "TEXT"),
    ("suppliers", "state", "TEXT"),
    ("suppliers", "postal_code", "TEXT"),
    ("suppliers", "latitude", "REAL"),
    ("suppliers", "longitude", "REAL"),
    ("suppliers", "active", "INTEGER NOT NULL DEFAULT 1"),
    ("suppliers", "updated_at", "TEXT"),
    # Data platform Phase 1 — RAW layer hash-rule versioning.
    ("raw_record", "payload_hash_version", "INTEGER NOT NULL DEFAULT 1"),
    # Data platform Phase 2 — ingestion telemetry. Left nullable on purpose:
    # runs recorded before Phase 2 genuinely do not have these values, and a
    # backfilled zero would be indistinguishable from a measured zero.
    ("ingestion_runs", "pipeline_run_id", "INTEGER REFERENCES pipeline_runs(id)"),
    ("ingestion_runs", "raw_batch_id", "INTEGER"),
    ("ingestion_runs", "connector_type", "TEXT"),
    ("ingestion_runs", "requested_since", "TEXT"),
    ("ingestion_runs", "duration_ms", "INTEGER"),
    ("ingestion_runs", "retries", "INTEGER"),
    ("ingestion_runs", "error_type", "TEXT"),
    ("ingestion_runs", "http_status", "INTEGER"),
    ("ingestion_runs", "source_rows", "INTEGER"),
    ("ingestion_runs", "duplicates_dropped", "INTEGER"),
    ("ingestion_runs", "records_unchanged", "INTEGER"),
    ("ingestion_runs", "raw_new", "INTEGER"),
    ("ingestion_runs", "raw_changed", "INTEGER"),
    ("ingestion_runs", "raw_unchanged", "INTEGER"),
    # Contractor intel Phase 3 — role attribution (additive).
    ("company_capabilities", "attribution_role", "TEXT"),
    ("company_capabilities", "attribution_confidence", "REAL"),
    ("company_capabilities", "capability_class", "TEXT"),
    ("company_capability_evidence", "attribution_role", "TEXT"),
    ("company_capability_evidence", "attribution_confidence", "REAL"),
    ("company_capability_evidence", "evidence_directness", "TEXT"),
    # Public onboarding — additive account fields on existing users tables.
    ("users", "account_kind", "TEXT NOT NULL DEFAULT 'employee'"),
    ("users", "account_state", "TEXT NOT NULL DEFAULT 'ACTIVE'"),
    ("users", "email_verified_at", "TEXT"),
    ("users", "business_name", "TEXT"),
    ("users", "business_category", "TEXT"),
]


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Idempotently add Sprint 2.1 columns to pre-existing tables."""
    for table, column, decl in _KNOWLEDGE_V21_COLUMNS:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if not exists:
            continue
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column in cols:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()


def seed_jurisdictions(conn: sqlite3.Connection) -> None:
    with open(JURISDICTIONS_YAML, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    rows = config.get("jurisdictions", [])
    for row in rows:
        notes = (row.get("notes") or "").strip()
        conn.execute(
            """
            INSERT INTO jurisdictions (slug, name, state, status, connector_type,
                                        endpoint_url, resource_id, notes)
            VALUES (:slug, :name, :state, :status, :connector_type,
                    :endpoint_url, :resource_id, :notes)
            ON CONFLICT(slug) DO UPDATE SET
                name=excluded.name,
                state=excluded.state,
                status=excluded.status,
                connector_type=excluded.connector_type,
                endpoint_url=excluded.endpoint_url,
                resource_id=excluded.resource_id,
                notes=excluded.notes
            """,
            {
                "slug": row["slug"],
                "name": row["name"],
                "state": row.get("state", "AZ"),
                "status": row["status"],
                "connector_type": row.get("connector_type"),
                "endpoint_url": row.get("endpoint_url"),
                "resource_id": row.get("resource_id"),
                "notes": notes,
            },
        )
    conn.commit()


def init_db() -> sqlite3.Connection:
    conn = get_connection()
    # Migrate pre-existing tables first so schema.sql indexes that reference new
    # columns (e.g. priority_score) can be created. On a fresh DB the tables do
    # not exist yet, so migrate_schema is a no-op and apply_schema builds the
    # full current shape.
    migrate_schema(conn)
    apply_schema(conn)
    seed_jurisdictions(conn)
    # Municipal Knowledge Engine tables + bootstrap dictionary seed.
    try:
        from pipeline.knowledge.seed import seed_knowledge_base

        seed_knowledge_base(conn)
    except Exception as exc:  # pragma: no cover - keep DB usable if seed fails
        print(f"Warning: knowledge seed skipped: {exc}")
    # Sprint 5 — secure CRM: default organization, roles, permissions.
    try:
        from pipeline.auth.seed import seed_auth

        seed_auth(conn)
    except Exception as exc:  # pragma: no cover - keep DB usable if seed fails
        print(f"Warning: auth seed skipped: {exc}")
    try:
        from pipeline.contractor_intel.enrichment import seed_enrichment_registry

        seed_enrichment_registry(conn)
    except Exception as exc:  # pragma: no cover
        print(f"Warning: enrichment registry seed skipped: {exc}")
    try:
        from pipeline.relevance.classify import seed_relevance_profiles

        seed_relevance_profiles(conn)
    except Exception as exc:  # pragma: no cover
        print(f"Warning: relevance profile seed skipped: {exc}")
    return conn


if __name__ == "__main__":
    connection = init_db()
    count = connection.execute("SELECT COUNT(*) AS n FROM jurisdictions").fetchone()["n"]
    print(f"Database initialized at {DB_PATH}")
    print(f"Seeded {count} jurisdictions.")
    connection.close()
