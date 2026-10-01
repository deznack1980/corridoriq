"""Tenant-store schema for customer books. Lives only in a supplier's own
tenant.db — never in the shared CorridorIQ intelligence database."""

BOOK_SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id          TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    source_filename   TEXT NOT NULL,          -- base name only, never the full path
    source_sha256     TEXT NOT NULL,
    source_bytes      INTEGER NOT NULL,
    imported_at       TEXT NOT NULL,
    row_count         INTEGER NOT NULL,
    accepted_count    INTEGER NOT NULL,
    rejected_count    INTEGER NOT NULL,
    warning_count     INTEGER NOT NULL,
    unknown_columns   TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_sha ON import_batches(source_sha256);

-- Supplier's values exactly as provided (trimmed only). One row per account ID;
-- each import is a full snapshot, so accounts absent from the latest batch are
-- kept but flagged in_latest_batch = 0.
CREATE TABLE IF NOT EXISTS book_accounts (
    supplier_account_id TEXT PRIMARY KEY,
    tenant_id           TEXT NOT NULL,
    company_name        TEXT NOT NULL,
    dba_name            TEXT,
    address             TEXT,
    city                TEXT,
    state               TEXT,
    postal_code         TEXT,
    phone               TEXT,
    roc_license         TEXT,
    branch              TEXT,
    assigned_rep        TEXT,
    last_purchase_date  TEXT,                -- as provided
    last_purchase_on    TEXT,                -- parsed ISO date, or NULL
    import_batch_id     TEXT NOT NULL REFERENCES import_batches(batch_id),
    source_row_number   INTEGER NOT NULL,
    in_latest_batch     INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

-- Derived matching keys, kept apart from the source values.
CREATE TABLE IF NOT EXISTS account_normalized (
    supplier_account_id TEXT PRIMARY KEY REFERENCES book_accounts(supplier_account_id) ON DELETE CASCADE,
    tenant_id           TEXT NOT NULL,
    name_key            TEXT,
    dba_key             TEXT,
    phone_key           TEXT,
    license_key         TEXT,
    street_key          TEXT,
    zip5                TEXT,
    city_key            TEXT,
    is_person_name      INTEGER NOT NULL DEFAULT 0,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS import_issues (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id       TEXT NOT NULL,
    batch_id        TEXT NOT NULL REFERENCES import_batches(batch_id),
    row_number      INTEGER NOT NULL,
    level           TEXT NOT NULL CHECK (level IN ('REJECTED', 'WARNING')),
    reason          TEXT NOT NULL,
    field_preview   TEXT
);

CREATE TABLE IF NOT EXISTS match_runs (
    run_id            TEXT PRIMARY KEY,
    tenant_id         TEXT NOT NULL,
    batch_id          TEXT NOT NULL REFERENCES import_batches(batch_id),
    rules_version     TEXT NOT NULL,
    intel_fingerprint TEXT NOT NULL,
    config_json       TEXT NOT NULL,
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    counts_json       TEXT
);

CREATE TABLE IF NOT EXISTS account_matches (
    run_id              TEXT NOT NULL REFERENCES match_runs(run_id),
    tenant_id           TEXT NOT NULL,
    supplier_account_id TEXT NOT NULL,
    confidence          TEXT NOT NULL CHECK (confidence IN
                          ('VERIFIED', 'HIGH_CONFIDENCE', 'REVIEW_REQUIRED', 'UNMATCHED')),
    company_id          INTEGER,             -- best candidate (for REVIEW: unconfirmed)
    evidence_json       TEXT NOT NULL,
    conflicts_json      TEXT NOT NULL,
    candidates_json     TEXT NOT NULL,
    review_reason       TEXT,
    PRIMARY KEY (run_id, supplier_account_id)
);

-- Human review decisions persist across runs (latest decision per account wins).
CREATE TABLE IF NOT EXISTS review_decisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id           TEXT NOT NULL,
    supplier_account_id TEXT NOT NULL REFERENCES book_accounts(supplier_account_id),
    decision            TEXT NOT NULL CHECK (decision IN ('CONFIRM', 'REJECT', 'SET_COMPANY')),
    company_id          INTEGER,
    reviewer            TEXT NOT NULL,
    note                TEXT,
    source_run_id       TEXT,
    decided_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_account ON review_decisions(supplier_account_id, id);

CREATE TABLE IF NOT EXISTS account_classifications (
    run_id              TEXT NOT NULL REFERENCES match_runs(run_id),
    tenant_id           TEXT NOT NULL,
    supplier_account_id TEXT NOT NULL,
    company_id          INTEGER,
    match_basis         TEXT NOT NULL,
    classification      TEXT NOT NULL,
    purchase_status     TEXT NOT NULL,
    market_status       TEXT NOT NULL,
    market_trend        TEXT NOT NULL,
    recent_permits      INTEGER,
    prior_permits       INTEGER,
    last_permit_date    TEXT,
    wet_relevant        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, supplier_account_id)
);

CREATE TABLE IF NOT EXISTS net_new_candidates (
    run_id           TEXT NOT NULL REFERENCES match_runs(run_id),
    tenant_id        TEXT NOT NULL,
    company_id       INTEGER NOT NULL,
    display_name     TEXT,
    city             TEXT,
    lanes            TEXT,
    recent_permits   INTEGER NOT NULL,
    last_permit_date TEXT,
    possible_account TEXT,                   -- an account under identity review that may be this company
    PRIMARY KEY (run_id, company_id)
);
"""
