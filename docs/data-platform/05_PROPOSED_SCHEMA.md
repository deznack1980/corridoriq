# 05 — Proposed Schema

**Status:** design proposal. This DDL is **not** applied and must not be applied until
Phase 2 is explicitly authorised.

All new objects are namespaced (`raw_`, `curated_`, `gold_`) so they cannot collide with
production tables. Nothing here renames, drops or alters an existing table.

Conventions: SQLite dialect, ISO-8601 UTC text timestamps (matching the existing
codebase), `INTEGER` booleans, `public_id` as an opaque ULID string for anything that may
ever be exposed externally.

---

## 1. RAW layer

```sql
-- One row per ingestion attempt against one source.
CREATE TABLE raw_ingest_batch (
    batch_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pipeline_run_id   INTEGER REFERENCES pipeline_runs(id),
    source_system     TEXT    NOT NULL,       -- 'phoenix_az', 'supplier_csv', ...
    source_entity_type TEXT   NOT NULL,       -- 'permit', 'product', ...
    connector_type    TEXT,                   -- 'arcgis_hub' | 'socrata' | 'csv'
    source_url        TEXT,                   -- exact endpoint queried
    requested_since   TEXT,                   -- incremental watermark used
    started_at        TEXT    NOT NULL,
    completed_at      TEXT,
    status            TEXT    NOT NULL DEFAULT 'running'
                      CHECK(status IN ('running','succeeded','failed','partial')),
    records_fetched   INTEGER NOT NULL DEFAULT 0,
    records_new       INTEGER NOT NULL DEFAULT 0,   -- hash not seen before
    records_changed   INTEGER NOT NULL DEFAULT 0,   -- hash differs from previous
    records_unchanged INTEGER NOT NULL DEFAULT 0,   -- hash identical, no row written
    error_message     TEXT
);
CREATE INDEX idx_raw_batch_source_time ON raw_ingest_batch(source_system, started_at DESC);
CREATE INDEX idx_raw_batch_run         ON raw_ingest_batch(pipeline_run_id);

-- Append-only. One row per OBSERVED VERSION of a source record.
-- Never UPDATE except to flip is_current 1 -> 0. Never DELETE.
CREATE TABLE raw_record (
    raw_record_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id           INTEGER NOT NULL REFERENCES raw_ingest_batch(batch_id),
    source_system      TEXT    NOT NULL,
    source_entity_type TEXT    NOT NULL,
    source_record_id   TEXT    NOT NULL,   -- natural id in the source (e.g. PER_NUM)
    payload_json       TEXT    NOT NULL,   -- verbatim source payload
    payload_hash       TEXT    NOT NULL,   -- sha256 of canonicalised payload_json
    source_url         TEXT,               -- per-record URL where derivable
    source_updated_at  TEXT,               -- source-side modification time, if exposed
    fetched_at         TEXT    NOT NULL,   -- when WE observed it
    version_number     INTEGER NOT NULL DEFAULT 1,
    is_current         INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_system, source_record_id, payload_hash)
);
CREATE INDEX idx_raw_record_current
    ON raw_record(source_system, source_record_id, is_current);
CREATE INDEX idx_raw_record_fetched ON raw_record(fetched_at);
CREATE INDEX idx_raw_record_batch   ON raw_record(batch_id);
```

**Write rule.** Compute `payload_hash` over the canonicalised payload. If it equals the
hash of the current row for that `(source_system, source_record_id)`, write nothing and
increment `records_unchanged`. Otherwise flip the old row's `is_current` to 0, insert a
new row with `version_number + 1`, and increment `records_changed`. The `UNIQUE` on
`(source_system, source_record_id, payload_hash)` also makes a re-run of the same batch
naturally idempotent.

This single rule eliminates the churn measured in `03` §7 (Goodyear: 245 real records
producing 12,250 phantom updates) and turns "what genuinely changed today" into an
indexed query.

---

## 2. Lineage spine

```sql
-- Binds every source record to the canonical entity it evidences,
-- with the method and confidence of that binding.
CREATE TABLE source_record_map (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system          TEXT    NOT NULL,
    source_record_id       TEXT    NOT NULL,
    raw_record_id          INTEGER REFERENCES raw_record(raw_record_id),
    canonical_entity_type  TEXT    NOT NULL,  -- 'company'|'permit'|'project'|'location'|'parcel'|'contact'
    canonical_entity_id    INTEGER NOT NULL,
    relationship_type      TEXT    NOT NULL,  -- 'is'|'contractor_of'|'owner_of'|'located_at'|'part_of'
    match_method           TEXT    NOT NULL,  -- see vocabulary below
    match_confidence       REAL    NOT NULL,  -- 0-100
    resolution_tier        TEXT    NOT NULL
                           CHECK(resolution_tier IN
                                 ('EXACT','HIGH_CONFIDENCE','PROBABLE','UNRESOLVED')),
    is_current             INTEGER NOT NULL DEFAULT 1,
    first_seen             TEXT    NOT NULL,
    last_seen              TEXT    NOT NULL,
    UNIQUE(source_system, source_record_id, canonical_entity_type,
           canonical_entity_id, relationship_type)
);
CREATE INDEX idx_srm_canonical ON source_record_map(canonical_entity_type, canonical_entity_id);
CREATE INDEX idx_srm_source    ON source_record_map(source_system, source_record_id);
CREATE INDEX idx_srm_tier      ON source_record_map(resolution_tier, match_confidence);
```

`match_method` vocabulary (extends the reason strings already emitted by
`company_resolution/match.py`):

`source_id_exact`, `permit_natural_key`, `name_exact`, `alias_exact`, `name_plus_license`,
`name_plus_phone`, `name_plus_city`, `phone_exact`, `email_domain`,
`heuristic_project_name`, `heuristic_permit_name`, `address_normalized`,
`parcel_exact`, `manual_review`, `manual_merge`.

The two `heuristic_*` methods exist specifically to label the Phoenix `PERMIT_NAME` and
Tempe `ProjectName` fallbacks, which today are stored identically to authoritative
contractor fields.

---

## 3. CURATED layer

### Reference entities

```sql
CREATE TABLE curated_jurisdiction (
    jurisdiction_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id              TEXT    NOT NULL UNIQUE,
    slug                   TEXT    NOT NULL UNIQUE,
    name                   TEXT    NOT NULL,
    state                  TEXT    NOT NULL,
    county                 TEXT,
    connector_type         TEXT,
    endpoint_url           TEXT,
    status                 TEXT    NOT NULL,   -- 'connected'|'pending'|'retired'
    -- governance (see 02 §7)
    license_code           TEXT,
    license_url            TEXT,
    attribution_text       TEXT,
    redistribution_allowed INTEGER,            -- NULL = unassessed, 0 = no, 1 = yes
    terms_reviewed_at      TEXT,
    -- coverage metadata, published with any market product
    coverage_start_date    TEXT,
    has_valuation          INTEGER,
    has_contractor         INTEGER,
    has_filed_date         INTEGER,
    created_at             TEXT    NOT NULL,
    updated_at             TEXT    NOT NULL
);

CREATE TABLE curated_location (
    location_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id          TEXT    NOT NULL UNIQUE,
    address_normalized TEXT    NOT NULL,   -- uppercased, standardised street form
    address_raw_first  TEXT,               -- first-observed raw spelling
    city               TEXT,
    state              TEXT,
    postal_code        TEXT,
    jurisdiction_id    INTEGER REFERENCES curated_jurisdiction(jurisdiction_id),
    parcel_id          INTEGER REFERENCES curated_parcel(parcel_id),
    latitude           REAL,
    longitude          REAL,
    geocode_source     TEXT,
    geocode_confidence REAL,
    first_seen         TEXT    NOT NULL,
    last_seen          TEXT    NOT NULL,
    created_at         TEXT    NOT NULL,
    updated_at         TEXT    NOT NULL,
    UNIQUE(jurisdiction_id, address_normalized)
);
CREATE INDEX idx_curated_location_parcel ON curated_location(parcel_id);
CREATE INDEX idx_curated_location_geo    ON curated_location(latitude, longitude);

CREATE TABLE curated_parcel (
    parcel_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id       TEXT    NOT NULL UNIQUE,
    apn             TEXT,
    parcel_number   TEXT,
    county          TEXT,
    state           TEXT    NOT NULL DEFAULT 'AZ',
    owner_entity_id INTEGER REFERENCES curated_company(company_id),
    land_use        TEXT,
    first_seen      TEXT    NOT NULL,
    last_seen       TEXT    NOT NULL,
    UNIQUE(state, county, parcel_number)
);
```

### Permit

```sql
CREATE TABLE curated_permit (
    permit_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    jurisdiction_id     INTEGER NOT NULL REFERENCES curated_jurisdiction(jurisdiction_id),
    permit_number       TEXT    NOT NULL,
    -- source-faithful values
    permit_type_raw     TEXT,
    permit_subtype_raw  TEXT,
    status_raw          TEXT,
    -- normalized values (dictionary-driven; NULL when unmapped, never guessed)
    permit_type_norm    TEXT,
    status_norm         TEXT,
    work_class          TEXT,
    description         TEXT,
    -- dates, ALL stored as strict 'YYYY-MM-DD' (fixes the Mesa format split)
    filed_date          TEXT,
    issued_date         TEXT,
    expiration_date     TEXT,
    finaled_date        TEXT,
    -- measures; NULL means unknown, 0 means genuinely zero
    valuation           REAL,
    valuation_is_source_null INTEGER NOT NULL DEFAULT 0,
    square_footage      REAL,
    -- links
    location_id         INTEGER REFERENCES curated_location(location_id),
    parcel_id           INTEGER REFERENCES curated_parcel(parcel_id),
    project_id          INTEGER REFERENCES curated_project(project_id),
    -- provenance
    raw_record_id       INTEGER REFERENCES raw_record(raw_record_id),
    source_system       TEXT    NOT NULL,
    source_record_id    TEXT    NOT NULL,
    source_url          TEXT,
    source_updated_at   TEXT,               -- distinct from our timestamps
    first_seen          TEXT    NOT NULL,
    last_seen           TEXT    NOT NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,   -- our processing time
    UNIQUE(jurisdiction_id, permit_number)
);
CREATE INDEX idx_curated_permit_issued   ON curated_permit(issued_date);
CREATE INDEX idx_curated_permit_status   ON curated_permit(status_norm);
CREATE INDEX idx_curated_permit_project  ON curated_permit(project_id);
CREATE INDEX idx_curated_permit_location ON curated_permit(location_id);
CREATE INDEX idx_curated_permit_updated  ON curated_permit(updated_at);
```

`valuation_is_source_null` exists because 73.9% of permits have null-or-zero valuation and
the two cases mean completely different things — Phoenix publishes no valuation field at
all, whereas a Scottsdale `0` is a reported zero. Collapsing them is how a market-size
product ends up wrong.

### Project — the new entity

```sql
CREATE TABLE curated_project (
    project_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id            TEXT    NOT NULL UNIQUE,
    project_name         TEXT,
    jurisdiction_id      INTEGER REFERENCES curated_jurisdiction(jurisdiction_id),
    location_id          INTEGER REFERENCES curated_location(location_id),
    parcel_id            INTEGER REFERENCES curated_parcel(parcel_id),
    -- how these permits were grouped into one job
    grouping_method      TEXT    NOT NULL,   -- 'parcel'|'address_time_window'|'single_permit'|'manual'
    grouping_confidence  REAL    NOT NULL,
    -- derived lifecycle over the whole permit set
    project_status       TEXT,               -- 'planned'|'permitted'|'active'|'completed'|'stalled'|'unknown'
    first_permit_date    TEXT,
    latest_permit_date   TEXT,
    completion_date      TEXT,
    permit_count         INTEGER NOT NULL DEFAULT 0,
    total_valuation      REAL,
    valuation_coverage_pct REAL,             -- share of permits that reported a valuation
    primary_category     TEXT,
    first_seen           TEXT    NOT NULL,
    last_seen            TEXT    NOT NULL,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);
CREATE INDEX idx_curated_project_location ON curated_project(location_id);
CREATE INDEX idx_curated_project_status   ON curated_project(project_status, latest_permit_date);

CREATE TABLE curated_project_permit (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER NOT NULL REFERENCES curated_project(project_id),
    permit_id           INTEGER NOT NULL REFERENCES curated_permit(permit_id),
    link_method         TEXT    NOT NULL,
    link_confidence     REAL    NOT NULL,
    is_primary_permit   INTEGER NOT NULL DEFAULT 0,
    first_seen          TEXT    NOT NULL,
    last_seen           TEXT    NOT NULL,
    UNIQUE(project_id, permit_id)
);
CREATE INDEX idx_cpp_permit ON curated_project_permit(permit_id);
```

### Company

```sql
CREATE TABLE curated_company (
    company_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id         TEXT    NOT NULL UNIQUE,
    legal_name        TEXT,
    display_name      TEXT    NOT NULL,
    normalized_name   TEXT    NOT NULL,
    dba_name          TEXT,
    company_type      TEXT,               -- 'contractor'|'owner'|'developer'|'architect'|'engineer'|'supplier'
    is_individual     INTEGER NOT NULL DEFAULT 0,   -- PII flag, see 03 §PII
    -- contact / firmographic (currently 100% empty; reserved for enrichment)
    website           TEXT,
    email_domain      TEXT,
    main_phone        TEXT,
    address_line_1    TEXT,
    city              TEXT,
    state             TEXT,
    postal_code       TEXT,
    latitude          REAL,
    longitude         REAL,
    license_number    TEXT,
    license_state     TEXT,
    license_status    TEXT,
    license_source    TEXT,
    -- identity management
    resolution_tier   TEXT    NOT NULL DEFAULT 'EXACT'
                      CHECK(resolution_tier IN
                            ('EXACT','HIGH_CONFIDENCE','PROBABLE','UNRESOLVED')),
    merged_into_id    INTEGER REFERENCES curated_company(company_id),
    lifecycle_state   TEXT    NOT NULL DEFAULT 'active',
    first_seen        TEXT    NOT NULL,
    last_seen         TEXT    NOT NULL,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX idx_curated_company_norm    ON curated_company(normalized_name);
CREATE INDEX idx_curated_company_license ON curated_company(license_number, license_state);
CREATE INDEX idx_curated_company_merged  ON curated_company(merged_into_id);

CREATE TABLE curated_company_alias (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id       INTEGER NOT NULL REFERENCES curated_company(company_id),
    alias_name       TEXT    NOT NULL,
    normalized_alias TEXT    NOT NULL,
    alias_type       TEXT,               -- 'observed'|'dba'|'legal'|'former'
    source_system    TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1,
    first_seen       TEXT    NOT NULL,
    last_seen        TEXT    NOT NULL,
    UNIQUE(company_id, normalized_alias)
);
CREATE INDEX idx_curated_alias_norm ON curated_company_alias(normalized_alias);

CREATE TABLE curated_contact (
    contact_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id      TEXT    NOT NULL UNIQUE,
    company_id     INTEGER REFERENCES curated_company(company_id),
    full_name      TEXT,
    job_title      TEXT,
    email          TEXT,
    phone          TEXT,
    is_pii         INTEGER NOT NULL DEFAULT 1,
    consent_basis  TEXT,
    source_system  TEXT,
    first_seen     TEXT    NOT NULL,
    last_seen      TEXT    NOT NULL
);
```

`is_individual` matters: 1,776 rows in today's `companies` are typed `property_owner` and
15,722 permits carry an `owner_name` with 10,826 distinct values. Many of those are private
individuals, not firms, and any external product must be able to exclude them.

### Project ↔ company relationships (per project, not global)

```sql
CREATE TABLE curated_project_company (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id         INTEGER NOT NULL REFERENCES curated_project(project_id),
    company_id         INTEGER NOT NULL REFERENCES curated_company(company_id),
    role               TEXT    NOT NULL,   -- 'general_contractor'|'plumbing_contractor'|
                                           -- 'owner'|'developer'|'architect'|'engineer'|'applicant'
    evidence_permit_id INTEGER REFERENCES curated_permit(permit_id),
    match_method       TEXT    NOT NULL,
    match_confidence   REAL    NOT NULL,
    is_verified        INTEGER NOT NULL DEFAULT 0,   -- authoritative source field vs heuristic
    effective_from     TEXT,
    effective_to       TEXT,
    first_seen         TEXT    NOT NULL,
    last_seen          TEXT    NOT NULL,
    UNIQUE(project_id, company_id, role)
);
CREATE INDEX idx_cpc_company ON curated_project_company(company_id, role);
CREATE INDEX idx_cpc_project ON curated_project_company(project_id);
```

This replaces three competing representations (`company_activity`'s 76,415 distinct pairs,
the five FK columns on `projects`, and the globally-unique `company_roles`) with one
per-project, confidence-scored, time-bounded relationship — and, via `is_verified`,
finally distinguishes Mesa's authoritative `contractor_name` from Phoenix's heuristic
`PERMIT_NAME` fallback.

---

## 4. Change-event tables

Narrow, append-only, one row per observed change to a tracked attribute.

```sql
CREATE TABLE curated_permit_change_event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    permit_id       INTEGER NOT NULL REFERENCES curated_permit(permit_id),
    attribute       TEXT    NOT NULL,   -- 'status_norm'|'valuation'|'issued_date'|
                                        -- 'finaled_date'|'contractor_company_id'
    old_value       TEXT,
    new_value       TEXT,
    changed_at      TEXT    NOT NULL,   -- source_updated_at when known, else observed_at
    observed_at     TEXT    NOT NULL,   -- when WE saw it
    raw_record_id   INTEGER REFERENCES raw_record(raw_record_id),
    batch_id        INTEGER REFERENCES raw_ingest_batch(batch_id)
);
CREATE INDEX idx_permit_change_permit ON curated_permit_change_event(permit_id, changed_at);
CREATE INDEX idx_permit_change_attr   ON curated_permit_change_event(attribute, changed_at);

CREATE TABLE curated_project_change_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES curated_project(project_id),
    attribute     TEXT    NOT NULL,   -- 'project_status'|'total_valuation'|'permit_count'
    old_value     TEXT,
    new_value     TEXT,
    changed_at    TEXT    NOT NULL,
    observed_at   TEXT    NOT NULL
);
CREATE INDEX idx_project_change ON curated_project_change_event(project_id, changed_at);

CREATE TABLE curated_company_change_event (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    INTEGER NOT NULL REFERENCES curated_company(company_id),
    attribute     TEXT    NOT NULL,
    old_value     TEXT,
    new_value     TEXT,
    changed_at    TEXT    NOT NULL,
    observed_at   TEXT    NOT NULL,
    changed_by    TEXT                -- 'pipeline' | user id for manual merges
);
CREATE INDEX idx_company_change ON curated_company_change_event(company_id, changed_at);
```

Expected volume: roughly 40–60k permit change events per year once no-op churn is
suppressed by hashing. Trivial for SQLite, and it answers "when did the valuation change"
and "when did the status change" directly, without scanning RAW payloads.

---

## 5. GOLD layer (first two only)

```sql
CREATE TABLE gold_company_profile (
    company_id              INTEGER PRIMARY KEY REFERENCES curated_company(company_id),
    public_id               TEXT    NOT NULL,
    display_name            TEXT    NOT NULL,
    company_type            TEXT,
    jurisdictions_active    INTEGER,
    total_projects          INTEGER,
    active_projects         INTEGER,
    projects_last_30d       INTEGER,
    projects_last_90d       INTEGER,
    projects_last_365d      INTEGER,
    total_valuation         REAL,
    valuation_coverage_pct  REAL,      -- honesty flag, given 73.9% null-or-zero
    first_activity_date     TEXT,
    latest_activity_date    TEXT,
    activity_trend          TEXT,
    verified_relationship_pct REAL,    -- share of links with is_verified = 1
    generated_at            TEXT    NOT NULL,
    source_watermark        TEXT    NOT NULL
);

CREATE TABLE gold_project_activity (
    project_id           INTEGER PRIMARY KEY REFERENCES curated_project(project_id),
    public_id            TEXT    NOT NULL,
    project_name         TEXT,
    jurisdiction_slug    TEXT,
    city                 TEXT,
    latitude             REAL,
    longitude            REAL,
    project_status       TEXT,
    permit_count         INTEGER,
    first_permit_date    TEXT,
    latest_permit_date   TEXT,
    days_active          INTEGER,
    total_valuation      REAL,
    valuation_coverage_pct REAL,
    primary_category     TEXT,
    general_contractor_id INTEGER,
    momentum_score       REAL,
    generated_at         TEXT    NOT NULL,
    source_watermark     TEXT    NOT NULL
);
```

`generated_at` and `source_watermark` are mandatory on every GOLD table: a data product
without a freshness guarantee is not sellable.

Note on scoring: `opportunity_score`, its weights and the 60-point threshold are **not
touched** by any of this. `gold_opportunity` will, when built, read the existing scoring
output verbatim.

---

## 6. Compatibility views

Once CURATED is populated and validated, these views let application code be repointed
one module at a time, with no change to the SQL shape it expects. This is what makes the
migration reversible.

```sql
-- Drop-in replacement shape for the existing `permits` table.
CREATE VIEW v_permits_compat AS
SELECT
    p.permit_id                AS id,
    j.slug                     AS jurisdiction,
    p.permit_number            AS permit_number,
    p.permit_type_raw          AS permit_type,
    p.status_raw               AS status,
    p.filed_date, p.issued_date, p.expiration_date, p.finaled_date,
    l.address_raw_first        AS job_address,
    l.city, l.state, l.postal_code AS zip,
    l.latitude, l.longitude,
    p.valuation, p.square_footage,
    p.first_seen               AS first_seen_at,
    p.updated_at               AS last_updated_at
FROM curated_permit p
JOIN curated_jurisdiction j ON j.jurisdiction_id = p.jurisdiction_id
LEFT JOIN curated_location l ON l.location_id = p.location_id;
```

An equivalent `v_companies_compat` maps `curated_company` onto the current `companies`
column names. Cutover per module becomes a one-line table-name change, and rollback is
the same one-line change in reverse.

---

## 7. Indexes to add to *existing* tables (non-breaking, safe today)

These are pure additions to production tables. They change no data and no behaviour, and
they address gaps found in the audit.

```sql
CREATE INDEX IF NOT EXISTS idx_ingestion_runs_juris_time
    ON ingestion_runs(jurisdiction_slug, run_started_at DESC);   -- table has NO indexes today
CREATE INDEX IF NOT EXISTS idx_permits_last_updated
    ON permits(last_updated_at);
CREATE INDEX IF NOT EXISTS idx_permits_first_seen
    ON permits(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_company_activity_project
    ON company_activity(project_id);                             -- 175,827 rows, no project index
CREATE INDEX IF NOT EXISTS idx_permits_address
    ON permits(job_address);                                     -- needed for grouping analysis
```

---

## 8. What is deliberately NOT in this schema

- **No Type-2 SCD on curated entities.** RAW plus change events covers the same questions
  at a fraction of the complexity. Revisit only if as-of reconstruction proves too slow.
- **No partitioning or sharding.** Unnecessary below ~10 GB.
- **No separate analytics store.** GOLD tables live in the same file until read contention
  is measured, not assumed.
- **No changes to `permits`, `projects`, `companies`, `company_activity`, `company_roles`,
  `contractors`, or any `crm_*` table.** They stay exactly as they are through the entire
  migration; `contractors` and `municipalities` are retired only after their consumers are
  repointed, and retirement means "stop writing", not "drop".
- **No change to scoring weights, inputs, or the 60-point threshold.**
