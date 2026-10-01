# 01 — Current State Audit

**Audit date:** 2026-09-15
**Method:** read-only introspection of the live database (`file:...?mode=ro` connections),
plus static reading of ingestion, analysis, API and UI source. No writes, no migrations,
no schema changes were performed.

---

## 1. Databases and engines

| Property | Value |
| --- | --- |
| Engine | SQLite 3 (single file, embedded, `sqlite3` stdlib driver) |
| Path | `pipeline/db/corridoriq.db` (resolved from `pipeline/config/settings.py:DB_PATH`) |
| Size | 282.91 MB |
| Tables | 43 |
| Views | 0 |
| Triggers | 0 |
| Total rows | 813,467 |
| `journal_mode` | `delete` (**not** WAL) |
| `foreign_keys` | Enabled per connection in `pipeline/db/database.py`. SQLite has **no** persistent file-level setting — `PRAGMA foreign_keys` defaults OFF on every new connection, so enforcement depends entirely on all code going through `get_connection()` |
| `user_version` | `0` (schema version is not stamped in the file) |
| `page_size` | 4096 |
| `auto_vacuum` | `0` (off — deleted pages are never reclaimed) |

There is exactly **one** database. There is no separate staging, warehouse, analytics or
archive store. There is no ORM and no migration framework; schema lives in
`pipeline/db/schema.sql` with hand-written additive `ALTER TABLE` migrations applied by
`pipeline/db/database.py`.

Derived artefacts exist as files rather than tables:

- `data/exports/*.json` — 14 JSON files consumed by the dashboards as an API fallback
- `data/exports/all_permits*.csv` — full-table CSV dumps
- `reports/generated/*` — Markdown / XLSX / PDF reports
- `logs/`, `logs/morning_refresh/` — run logs

### Material infrastructure finding

The database file lives inside a **OneDrive-synchronised folder**
(`C:\Users\dezna\OneDrive\Desktop\CorridorIQ`). A 283 MB SQLite file under continuous
cloud sync, in rollback (`delete`) journal mode, with a long-running API server process
and a scheduled write-heavy pipeline, is exposed to partial-file sync, `.db-journal`
divergence and lock contention. This is the single largest operational risk to the data
asset and is unrelated to schema design.

---

## 2. Table inventory

Row counts are actual `COUNT(*)` values as of the audit.

### Core data (the asset)

| Table | Rows | Notes |
| --- | ---: | --- |
| `permits` | 110,298 | Source-of-record permit rows, one per `(jurisdiction, permit_number)` |
| `projects` | 110,298 | **1:1 with permits** — analysis/scoring row, not a real project entity |
| `companies` | 12,636 | Canonical company registry |
| `company_activity` | 175,827 | Event rows linking company ↔ project/permit |
| `company_aliases` | 12,636 | Exactly one alias per company; **0** differ from the canonical name |
| `company_intelligence` | 12,636 | Derived per-company metrics (1:1 with companies) |
| `company_roles` | 12,794 | contractor / property_owner role assignment |
| `company_identity_audit_log` | 12,637 | Identity decisions log |
| `company_match_review_queue` | 44 | 43 pending, 1 approved |
| `contractors` | 11,034 | **Legacy parallel entity**, overlaps `companies` |
| `estimated_materials` | 334,076 | Derived BOM estimate per project |
| `contacts` | 0 | Empty |
| `jurisdictions` | 13 | 9 connected, 4 pending |
| `municipalities` | 16 | Overlapping/duplicate registry vs `jurisdictions` |

### Knowledge / normalization

| Table | Rows |
| --- | ---: |
| `keyword_dictionary` | 62 |
| `permit_code_dictionary` | 24 |
| `status_dictionary` | 17 |
| `product_dictionary` | 14 |
| `knowledge_review_queue` | 422 |
| `knowledge_meta` | 2 |
| `mapping_audit_log` | 2 |

### Operations / run tracking

| Table | Rows |
| --- | ---: |
| `ingestion_runs` | 533 |
| `pipeline_runs` | 4 |
| `security_audit_log` | 57 |
| `supplier_import_log` | 2 |

### CRM (multi-tenant, user-owned)

| Table | Rows |
| --- | ---: |
| `crm_company_relationships` | 1 |
| `crm_activities` | 2 |
| `crm_assignment_history` | 2 |
| `crm_tasks` | 1 |
| `crm_activity_revisions` | 0 |

### Supplier / product

| Table | Rows |
| --- | ---: |
| `products` | 3,600 |
| `supplier_products` | 3,600 |
| `suppliers` | 1 |
| `pricing_config` | 0 |
| `quotes` | 0 |
| `deliveries` | 0 |

### Auth / tenancy

| Table | Rows |
| --- | ---: |
| `users` | 4 |
| `roles` | 6 |
| `permissions` | 36 |
| `role_permissions` | 95 |
| `user_roles` | 4 |
| `sessions` | 30 |
| `organizations` | 1 |

---

## 3. Keys, constraints and indexes

### `permits` (36 columns)

- PK: `id INTEGER` (rowid alias)
- Natural key: `UNIQUE(jurisdiction, permit_number)` — this is the deduplication contract
- FKs: `contractor_company_id → companies(id)`, `jurisdiction → jurisdictions(slug)`
- Indexes: `contractor_company_id`, `parcel_number`, `city`, `status`, `issued_date`, `jurisdiction`
- NOT NULL: `jurisdiction`, `first_seen_at`, `last_updated_at` only
- Source fields: `raw_source_json` (overwritten on update), `permit_url`
- Timestamps: `first_seen_at`, `last_updated_at`. **No `source_updated_at`.**

### `projects` (23 columns)

- PK: `id`; `UNIQUE(permit_id)` → hard 1:1 with `permits`
- FKs to `permits`, `contractors`, and five company-role columns
  (`contractor_/owner_/developer_/architect_/engineer_company_id`)
- Carries all scoring output: `opportunity_score`, `confidence_score`,
  `estimated_material_value`, `project_lifecycle`, `analysis_version`, `analyzed_at`
- **No history.** All 110,298 rows share `analyzed_at = 2026-09-15` and a single
  `analysis_version` — every analysis pass overwrites every row in place.

### `companies` (32 columns)

- PK: `id`; `normalized_name` NOT NULL
- Indexes: `normalized_name` (**non-unique**), `license_number`, `(state, city)`,
  `(source_system, source_record_id)`, `merged_into_id`
- Merge support present: `merged_into_id`, `lifecycle_state` (0 rows currently merged)
- Lineage columns exist (`source_system`, `source_record_id`) but are effectively
  unpopulated: **all 12,636 rows have `source_system = 'permits'`**

### `company_activity` (14 columns)

- `UNIQUE(dedupe_key)` — idempotency is enforced by a synthetic key
- FKs to `companies`, `projects`, `permits`, `contacts`
- This is the de-facto project↔company relationship table: **76,415 distinct
  `(company_id, project_id)` pairs** — this is the "70,000+ relationships" figure

### `company_roles`

- `UNIQUE(company_id, role_type)` — a company gets one row per role **globally**, not
  per project. `confidence` and `effective_from/to` columns exist but are **NULL for
  every row**.

### Referential integrity

`PRAGMA foreign_key_check` over the whole database returned **0 violations**. Spot checks
for orphans (`projects→permits`, `company_activity→companies/projects`,
`estimated_materials→projects`, `permits.contractor_company_id→companies`,
`companies.merged_into_id`) all returned 0. Referential integrity is genuinely clean.

### Missing indexes / constraints worth noting

- `ingestion_runs` has **no indexes at all** (533 rows today; grows every run)
- `companies.normalized_name` is indexed but **not unique** — nothing structurally
  prevents duplicate canonical companies; today's 0 duplicates are a product of
  application discipline, not the schema
- No `CHECK` constraints on date columns, valuation, or score ranges
- No unique constraint tying a permit to a parcel or location

---

## 4. Entity model as implemented

| Business concept | Implementation | Assessment |
| --- | --- | --- |
| Permit | `permits` | Solid. Real natural key, honest source mapping. |
| Project | `projects` (1:1 with permit) | **Not a project.** It is a scoring row. |
| Company | `companies` + `company_aliases` + `company_roles` | Good bones, thin data. |
| Contractor | `contractors` **and** `companies.company_type_primary='contractor'` | **Duplicated entity.** |
| Address / Location | Denormalised strings on `permits` | **No entity.** |
| Parcel | `permits.parcel_number` / `permits.apn` | **No entity.** `apn` is 100% NULL. |
| Jurisdiction | `jurisdictions` **and** `municipalities` | **Duplicated registry.** |
| Contact | `contacts` | Table exists, 0 rows. |
| Supplier / Material | `suppliers`, `products`, `supplier_products`, `estimated_materials` | Separate island, no link to companies. |
| Opportunity | Columns on `projects` | Not a first-class, historised entity. |
| Relationship | `company_activity` + 5 FK columns on `projects` + `company_roles` | **Three competing representations.** |

### The central entity-model gap

`projects` is `UNIQUE(permit_id)` — one row per permit, always. But the data shows real
multi-permit projects:

- 110,298 permits occupy only **48,103 distinct addresses**
- **16,644 addresses carry more than one permit**
- **63,570 permits (57.6%)** sit on an address shared with at least one other permit
- Extreme cases: `32200 N 43RD AVE` (634 permits), `5088 W INNOVATION CIR` (410),
  `2800 W SONORAN DESERT DR` (293)

A single large development is currently represented as hundreds of unrelated "projects",
each scored independently. Every question of the form *"how big is this job, who is on
it, how long has it been running"* is unanswerable in the current model.

### Company duplication risk

- `companies.normalized_name` duplicates: **0 groups** (clean today)
- `companies.license_number` duplicates: 1 group
- But `contractors` holds 11,034 rows of which **748 have no matching company** by
  normalized name — a second, drifting representation of the same real-world entity
- `company_aliases` has 12,636 rows and **0** whose `normalized_alias` differs from the
  parent's `normalized_name`. The alias mechanism is built but carries no information,
  so no real-world naming variation is currently captured.

---

## 5. History and versioning behaviour

This is the weakest area of the current platform.

| Capability | Status |
| --- | --- |
| `first_seen` | Present on `permits`, `companies`, `company_aliases`, `contacts` |
| `last_seen` | `permits.last_updated_at`, `companies.last_seen_at` |
| `source_updated_at` | **Does not exist on any table** |
| Ingestion timestamp distinct from change timestamp | **No** |
| Prior values retained on update | **No** — `UPDATE` in place |
| Raw payload retained per version | **No** — `raw_source_json` is overwritten |
| Status-change history | **No** |
| Valuation-change history | **No** |
| Score/lifecycle history | **No** — single `analyzed_at`, single `analysis_version` |
| Relationship history | Partly — `company_activity` is append-only and dated |
| Run-level history | Yes — `ingestion_runs` (533), `pipeline_runs` (4) |

Measured evidence:

- **52,755 permits (47.8%)** have `last_updated_at <> first_seen_at`, i.e. they have been
  overwritten at least once with no prior value retained.
- All **110,298** projects carry `analyzed_at = 2026-09-15`: the entire scoring history of
  the business is exactly one day old, by construction.
- `permits.first_seen_at` spans only **25 distinct days**, so even acquisition history is
  coarse.

### `last_updated_at` is not a trustworthy change signal

Two independent problems corrupt it:

1. **No-op churn.** Connectors without a working incremental date filter re-fetch and
   re-`UPDATE` every row each run. Cumulative `ingestion_runs` totals:
   `goodyear_az` 245 real records → **12,250 recorded updates** over 54 runs;
   `chandler_az` 85 records → **8,755 updates** over 59 runs. Nothing changed; the
   timestamp moved anyway.
2. **Internal enrichment bumps the same column.** On the audit date only 486 records were
   fetched from sources, yet **8,140 permits** have `last_updated_at = today` — the
   difference comes from company backfill writing `contractor_company_id`. Source change
   time and internal processing time are conflated into one column.

`pipeline/ingestion/upsert.py` gained a `detect_unchanged` flag for the morning refresh,
which suppresses some of this on that path only. The general `ingest` path still churns.

### Questions that currently cannot be answered

- When did this project first appear? — only approximately, via `first_seen_at`, and only
  at permit granularity.
- When did its valuation change? — **unanswerable**, prior values are gone.
- When was a contractor first associated with it? — partially, via `company_activity`
  dates, which derive from permit dates rather than observation dates.
- How long was the project active? — **unanswerable**, no project entity and no state
  timeline.
- When did the permit status change? — **unanswerable**, only the current status exists.
- Which companies increased activity over time? — only via the pre-computed
  `permit_growth_30d/90d/12m` columns, which are themselves overwritten each run and
  cannot be recomputed for a past date.

---

## 6. Application dependencies

Every consumer reaches the database through raw SQL with hardcoded table and column
names. There is no repository layer, no view layer and no schema abstraction, so any
rename is a breaking change across the codebase.

Approximate count of modules issuing `FROM <core table>` statements (38 modules total):

| Module | Statements | Role |
| --- | ---: | --- |
| `pipeline/crm/service.py` | 40 | Sales workspace, opportunities, activity, follow-ups |
| `pipeline/crm/admin.py` | 23 | Admin dashboard, users, team, assignments |
| `pipeline/company_resolution/queries.py` | 14 | Company profile / search |
| `pipeline/tests/test_company_intelligence.py` | 14 | Tests |
| `pipeline/export/export_json.py` | 11 | All 14 dashboard JSON exports |
| `pipeline/company_resolution/reports.py` | 11 | Identity reports |
| `pipeline/reports/*` | ~15 | Markdown / XLSX / PDF generation |
| `pipeline/analysis/run_analysis.py` | 3 | Scoring |
| `pipeline/contractors/rebuild.py` | 4 | Legacy contractor rebuild |
| `pipeline/products/routing.py` | 2 | Supplier routing |

### API surface (`pipeline/api/server.py`)

`GET`: `/api/health`, `/api/auth/me`, `/api/sales/dashboard`, `/api/admin/dashboard`,
`/api/estimator/work-queue`, `/api/sales/companies`, `/api/sales/opportunities`,
`/api/sales/activity`, `/api/sales/followups`, `/api/sales/tasks`, `/api/manager/team`,
`/api/manager/assignments`, `/api/admin/users`, `/api/status/refresh`,
`/api/admin/morning-refresh`, `/api/reports/catalog`, `/api/reports/download`,
`/api/products/search`, `/api/admin/suppliers`, `/api/admin/catalog/imports`,
`/api/admin/pricing/delivery`

`POST`: `/api/auth/login`, `/api/auth/logout`, `/api/auth/change-password`,
`/api/sales/tasks`, `/api/manager/assignments`, `/api/admin/morning-refresh/run`,
`/api/admin/users`, `/api/products/quote`, `/api/admin/suppliers`,
`/api/admin/catalog/preview`, `/api/admin/catalog/import`, `PATCH /api/admin/pricing/delivery`

All of these are internal, session-authenticated and RBAC-gated. None is a public data API.

### Hard dependencies that constrain migration

- `projects.opportunity_score >= 60` is the threshold used across CRM queries, exports and
  reports. The **60-point threshold and scoring weights must not move.**
- `projects.permit_id` 1:1 is assumed by joins throughout `crm/service.py`,
  `export_json.py` and the report generators.
- `company_intelligence.company_priority_score` / `_tier` drive the sales prioritisation UI.
- Dashboards read `data/exports/*.json` when the API is unavailable, so export shape is a
  second contract.
- CRM tables carry `organization_id` — the only multi-tenant boundary in the system, and
  the only place user-generated (non-source) data lives.

---

## 7. What is genuinely strong

Worth stating plainly, because the rebuild should preserve these:

1. **Honest source discipline.** Connectors raise `ConnectorNotConfiguredError` rather
   than returning empty or fabricated data. Gaps are documented per jurisdiction in
   `jurisdictions.yaml` instead of being silently filled.
2. **Real natural keys.** `UNIQUE(jurisdiction, permit_number)` and
   `company_activity.dedupe_key` make ingestion genuinely idempotent.
3. **Clean referential integrity.** Zero FK violations across 813k rows.
4. **Conservative entity resolution.** `company_resolution/match.py` is deterministic,
   tiered, treats license numbers as corroborating-only (because of shared placeholder
   values like `HTE0001`), and never auto-merges below threshold. This is the right
   philosophy and should be carried forward unchanged.
5. **Run tracking and audit logs** already exist (`ingestion_runs`, `pipeline_runs`,
   `company_identity_audit_log`, `security_audit_log`, `mapping_audit_log`).
6. **Real RBAC and tenancy** on the application side.
7. **The raw payload is captured at all** (`raw_source_json`) — it is overwritten, but the
   ingestion code already knows how to carry it, so a RAW layer is a small change.
