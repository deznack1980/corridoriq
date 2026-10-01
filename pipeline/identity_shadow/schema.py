"""Shadow database schema.

Layers (never mixed):
  source layer   shadow_batches, source_provenance, source_records,
                 source_identities, identity_evidence
  identity layer identity_signatures, canonical_companies, company_names,
                 company_name_relationships, company_addresses, company_phones,
                 company_emails, company_domains, company_licenses,
                 resolution_decisions, resolution_reviews
  activity layer company_activity_links (activity never defines existence)
  analysis       shadow_diff, shadow_metrics

Raw payloads are NOT copied: source_records reference the production row
(table + id + version + payload hash). Source/evidence rows have deterministic
IDs (idempotent re-runs). Identity/activity/analysis rows are derived and are
rebuilt wholesale on every run.
"""

SHADOW_PURPOSE = "identity_evidence_shadow"

DERIVED_TABLES = (
    "identity_signatures", "identity_links", "canonical_companies", "company_names", "company_name_relationships",
    "company_addresses", "company_phones", "company_emails", "company_domains", "company_licenses",
    "resolution_decisions", "resolution_reviews", "company_activity_links", "shadow_diff",
    "shadow_metrics",
)

SHADOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_meta (
    singleton  INTEGER PRIMARY KEY CHECK (singleton = 1),
    purpose    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shadow_batches (
    batch_id            TEXT PRIMARY KEY,
    started_at          TEXT NOT NULL,
    finished_at         TEXT,
    status              TEXT NOT NULL,
    parser_version      TEXT NOT NULL,
    normalizer_version  TEXT NOT NULL,
    rules_version       TEXT NOT NULL,
    config_json         TEXT NOT NULL,
    production_fingerprint TEXT,
    counts_json         TEXT,
    timings_json        TEXT
);

CREATE TABLE IF NOT EXISTS source_provenance (
    family          TEXT PRIMARY KEY,
    family_class    TEXT NOT NULL,
    independent     INTEGER NOT NULL,
    origin          TEXT NOT NULL,
    usage_terms     TEXT NOT NULL DEFAULT 'UNKNOWN'
);

CREATE TABLE IF NOT EXISTS source_records (
    record_key        TEXT PRIMARY KEY,           -- deterministic
    family            TEXT NOT NULL,
    source_system     TEXT NOT NULL,
    source_record_id  TEXT NOT NULL,
    prod_table        TEXT NOT NULL,              -- production table referenced (no payload copy)
    prod_row_id       INTEGER,
    version           INTEGER NOT NULL DEFAULT 1,
    is_current        INTEGER NOT NULL DEFAULT 1,
    payload_hash      TEXT,
    observed_at       TEXT,
    first_batch_id    TEXT NOT NULL,
    last_batch_id     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_srcrec_family ON source_records(family);

CREATE TABLE IF NOT EXISTS source_identities (
    identity_id       TEXT PRIMARY KEY,           -- deterministic
    record_key        TEXT NOT NULL REFERENCES source_records(record_key),
    family            TEXT NOT NULL,
    role              TEXT NOT NULL,
    entity_kind       TEXT NOT NULL CHECK (entity_kind IN ('BUSINESS','PERSON','UNKNOWN')),
    eligible          INTEGER NOT NULL,           -- may it seed / join a canonical company?
    state             TEXT NOT NULL CHECK (state IN ('CURRENT','SUPERSEDED')),
    original_name     TEXT NOT NULL,              -- exactly as the source wrote it
    name_key          TEXT,
    dba_original      TEXT,
    dba_key           TEXT,
    phone_key         TEXT,
    street_key        TEXT,
    zip5              TEXT,
    city_key          TEXT,
    email_key         TEXT,
    domain_key        TEXT,
    license_key       TEXT,
    prod_company_id   INTEGER,                    -- production company this identity is linked to today
    prod_permit_id    INTEGER,
    observed_at       TEXT,
    parser_version    TEXT NOT NULL,
    normalizer_version TEXT NOT NULL,
    first_batch_id    TEXT NOT NULL,
    last_batch_id     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ident_family ON source_identities(family);
CREATE INDEX IF NOT EXISTS idx_ident_prod ON source_identities(prod_company_id);
CREATE INDEX IF NOT EXISTS idx_ident_permit ON source_identities(prod_permit_id);

CREATE TABLE IF NOT EXISTS identity_evidence (
    evidence_id        TEXT PRIMARY KEY,          -- deterministic
    identity_id        TEXT NOT NULL REFERENCES source_identities(identity_id),
    family             TEXT NOT NULL,
    evidence_type      TEXT NOT NULL,             -- name, dba, phone, address, email, domain, license,
                                                  -- license_class, contact_person, qualifying_party
    value_key          TEXT NOT NULL,             -- normalized value
    original_value     TEXT,
    strength           TEXT NOT NULL CHECK (strength IN ('STRONG','CORROBORATING','WEAK','ATTRIBUTE')),
    source_status      TEXT,                      -- e.g. VERIFIED / CANDIDATE as the source recorded it
    state              TEXT NOT NULL CHECK (state IN ('CURRENT','STALE','SUPERSEDED')),
    observed_at        TEXT,
    ingested_at        TEXT NOT NULL,
    normalizer_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ev_identity ON identity_evidence(identity_id);
CREATE INDEX IF NOT EXISTS idx_ev_type_value ON identity_evidence(evidence_type, value_key);

-- ---------------- derived identity layer (rebuilt every run) ----------------
CREATE TABLE IF NOT EXISTS identity_signatures (
    signature_id   TEXT PRIMARY KEY,               -- dedupe within source
    family         TEXT NOT NULL,
    entity_kind    TEXT NOT NULL,
    identity_count INTEGER NOT NULL,
    canonical_id   TEXT,                           -- NULL = held unresolved (e.g. person name only)
    link_state     TEXT NOT NULL                    -- how this signature joined its company
);
CREATE INDEX IF NOT EXISTS idx_sig_canonical ON identity_signatures(canonical_id);

CREATE TABLE IF NOT EXISTS identity_links (
    identity_id   TEXT PRIMARY KEY,                -- lineage: source identity → signature → company
    signature_id  TEXT,
    canonical_id  TEXT,                            -- NULL = held (not a company)
    link_state    TEXT NOT NULL,
    hold_reason   TEXT
);
CREATE INDEX IF NOT EXISTS idx_links_canonical ON identity_links(canonical_id);

CREATE TABLE IF NOT EXISTS canonical_companies (
    canonical_id            TEXT PRIMARY KEY,
    display_name            TEXT NOT NULL,
    entity_kind             TEXT NOT NULL,         -- BUSINESS / SOLE_PROPRIETOR / UNKNOWN
    confidence_state        TEXT NOT NULL CHECK (confidence_state IN
                              ('SOURCE_ONLY','CANDIDATE','HIGH_CONFIDENCE','VERIFIED','CONFLICT','REVIEW_REQUIRED')),
    family_count            INTEGER NOT NULL,
    independent_family_count INTEGER NOT NULL,
    corroborated_family_count INTEGER NOT NULL,    -- families joined by identifying evidence
    signature_count         INTEGER NOT NULL,
    identity_count          INTEGER NOT NULL,
    has_license             INTEGER NOT NULL,
    licensed_without_observed_activity INTEGER NOT NULL,
    permit_count            INTEGER NOT NULL,
    recent_permit_count     INTEGER NOT NULL,
    last_activity_date      TEXT,
    prod_company_count      INTEGER NOT NULL,
    rules_version           TEXT NOT NULL,
    batch_id                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cc_state ON canonical_companies(confidence_state);

CREATE TABLE IF NOT EXISTS company_names (
    canonical_id TEXT NOT NULL, name_key TEXT NOT NULL, original TEXT,
    name_type TEXT NOT NULL,       -- LEGAL / DBA / OBSERVED / HISTORICAL
    families TEXT NOT NULL, identity_count INTEGER NOT NULL,
    PRIMARY KEY (canonical_id, name_key, name_type)
);
CREATE TABLE IF NOT EXISTS company_name_relationships (
    canonical_id TEXT NOT NULL, legal_name_key TEXT NOT NULL, dba_name_key TEXT NOT NULL,
    relationship TEXT NOT NULL,    -- DBA_OF
    families TEXT NOT NULL,
    PRIMARY KEY (canonical_id, legal_name_key, dba_name_key)
);
CREATE TABLE IF NOT EXISTS company_addresses (
    canonical_id TEXT NOT NULL, street_key TEXT NOT NULL, zip5 TEXT NOT NULL DEFAULT '', city_key TEXT,
    families TEXT NOT NULL, evidence_count INTEGER NOT NULL, shared INTEGER NOT NULL, state TEXT NOT NULL,
    PRIMARY KEY (canonical_id, street_key, zip5)
);
CREATE TABLE IF NOT EXISTS company_phones (
    canonical_id TEXT NOT NULL, phone_key TEXT NOT NULL, families TEXT NOT NULL,
    evidence_count INTEGER NOT NULL, shared INTEGER NOT NULL, verified INTEGER NOT NULL, state TEXT NOT NULL,
    PRIMARY KEY (canonical_id, phone_key)
);
CREATE TABLE IF NOT EXISTS company_emails (
    canonical_id TEXT NOT NULL, email_key TEXT NOT NULL, families TEXT NOT NULL,
    evidence_count INTEGER NOT NULL, verified INTEGER NOT NULL, state TEXT NOT NULL,
    PRIMARY KEY (canonical_id, email_key)
);
CREATE TABLE IF NOT EXISTS company_domains (
    canonical_id TEXT NOT NULL, domain_key TEXT NOT NULL, families TEXT NOT NULL,
    evidence_count INTEGER NOT NULL, shared INTEGER NOT NULL, state TEXT NOT NULL,
    PRIMARY KEY (canonical_id, domain_key)
);
CREATE TABLE IF NOT EXISTS company_licenses (
    canonical_id TEXT NOT NULL, license_key TEXT NOT NULL, license_class TEXT,
    families TEXT NOT NULL, state TEXT NOT NULL,
    PRIMARY KEY (canonical_id, license_key)
);

CREATE TABLE IF NOT EXISTS resolution_decisions (
    decision_id   TEXT PRIMARY KEY,            -- deterministic: rules version + subject
    subject       TEXT NOT NULL,               -- signature or pair being decided
    canonical_id  TEXT,
    decision      TEXT NOT NULL,               -- LINK_STRONG / LINK_HIGH / ATTACH_CANDIDATE / HOLD / CONFLICT / REVIEW
    rule          TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    conflicts_json TEXT NOT NULL,
    rules_version TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dec_canonical ON resolution_decisions(canonical_id);

CREATE TABLE IF NOT EXISTS resolution_reviews (
    review_id        TEXT PRIMARY KEY,
    review_kind      TEXT NOT NULL,
    subject          TEXT NOT NULL,
    candidate_canonicals TEXT NOT NULL,
    supporting_evidence TEXT NOT NULL,
    conflicting_evidence TEXT NOT NULL,
    families         TEXT NOT NULL,
    reason           TEXT NOT NULL,
    suggested_action TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS company_activity_links (
    canonical_id   TEXT NOT NULL,
    prod_permit_id INTEGER NOT NULL,
    role           TEXT NOT NULL,
    link_state     TEXT NOT NULL,
    activity_date  TEXT,
    PRIMARY KEY (canonical_id, prod_permit_id, role)
);
CREATE INDEX IF NOT EXISTS idx_act_permit ON company_activity_links(prod_permit_id);

CREATE TABLE IF NOT EXISTS shadow_diff (
    diff_kind      TEXT NOT NULL,               -- WOULD_SPLIT / WOULD_MERGE / UNREPRESENTED / ...
    subject        TEXT NOT NULL,
    detail_json    TEXT NOT NULL,
    PRIMARY KEY (diff_kind, subject)
);

CREATE TABLE IF NOT EXISTS shadow_metrics (
    metric   TEXT NOT NULL,
    scope    TEXT NOT NULL DEFAULT 'all',
    value    REAL NOT NULL,
    PRIMARY KEY (metric, scope)
);
"""
