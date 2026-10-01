# 02 — Source Lineage

**Audit date:** 2026-09-15

This document maps every byte of CorridorIQ data from its origin to the screen, and
identifies exactly where traceability is lost today.

---

## 1. Source registry

Sources are declared in `pipeline/config/jurisdictions.yaml` and loaded into the
`jurisdictions` table. A jurisdiction is only marked `connected` after a live endpoint has
been verified to return real individual records.

### Connected (9 of 13)

| Slug | Connector | Endpoint | Permits | Date span (issued) | Incremental filter |
| --- | --- | --- | ---: | --- | --- |
| `phoenix_az` | `arcgis_hub` | `maps.phoenix.gov/pub/rest/services/Public/Planning_Permit/MapServer/1` | 49,510 | 2024-07-15 → 2026-09-14 | `PER_ISSUE_DATE` |
| `scottsdale_az` | `arcgis_hub` | `maps.scottsdaleaz.gov/arcgis/rest/services/OpenData_Tabular/MapServer/12` | 15,484 | 2024-07-09 → 2026-07-10 | `IssueDate` |
| `buckeye_az` | `arcgis_hub` | `maps.buckeyeaz.gov/server/rest/services/Hosted/EnergovPermitswReviewHistory2/FeatureServer/0` | 13,267 | 2024-07-15 → 2026-08-16 | `issuedate` |
| `gilbert_az` | `arcgis_hub` | `maps.gilbertaz.gov/arcgis/rest/services/OD/Growth_Development_Tables_1/MapServer/3` | 12,101 | 2024-07-08 → 2026-09-10 | `IssuedDate` |
| `mesa_az` | `socrata` | `data.mesaaz.gov` resource `m2kk-w2hz` | 10,485 | 2024-07-09 → 2026-09-10 | SoDA `$where` |
| `tempe_az` | `arcgis_hub` | `services.arcgis.com/lQySeXwbBg53XWDi/.../building_permits/FeatureServer/0` | 5,786 | 2024-07-09 → 2026-09-03 | `IssuedDateDtm` |
| `peoria_az` | `arcgis_hub` | `gis.peoriaaz.gov/arcgis/rest/services/Accela/Peoria_Building_Permit_All/FeatureServer/3` | 3,335 | 2025-07-10 → 2026-09-14 | `IssDate` |
| `goodyear_az` | `arcgis_hub` | `maps.goodyearaz.gov/server/rest/services/Accela/Construction_Permits/MapServer/0` | 245 | 2021-11-15 → 2024-11-20 | **none — full refresh** |
| `chandler_az` | `arcgis_hub` | `gis.chandleraz.gov/appsanonymous/rest/services/DevelopmentServices/DSActiveProjects/MapServer/18` | 85 | 2019-09-16 → 2025-10-17 | **none — full refresh** |

### Pending (4 of 13)

`glendale_az` (GIS folders return 499 Token Required), `surprise_az` (no per-permit
dataset; DNS dead ends), `avondale_az` (Accela behind token), `queen_creek_az`
(Accela Citizen Access UI only). Each carries a dated re-check note in the YAML.

### Non-permit sources

| Source | Path | Loaded into |
| --- | --- | --- |
| Supplier price list (Magento-style CSV) | manual upload via `/api/admin/catalog/import` or `python -m pipeline.run import_supplier_catalog` | `products`, `supplier_products`, `supplier_import_log` |
| Knowledge dictionaries | seeded + curated in-app | `status_dictionary`, `permit_code_dictionary`, `keyword_dictionary`, `product_dictionary` |
| CRM activity | human input via API | `crm_*` tables |

---

## 2. Source-to-table lineage map

Example trace for the largest source:

```
City of Phoenix Planning_Permit MapServer layer 1
  │  HTTP GET /query?where=PER_ISSUE_DATE >= TIMESTAMP '<last_synced_at>'
  │          &outFields=*&f=json&resultOffset=N&resultRecordCount=1000
  │  connector: pipeline/connectors/arcgis_hub.py :: build_phoenix_connector()
  ▼
raw feature.attributes dict  ─────────────────────────────  (in memory only, never persisted as-is)
  │  map_record() applies PHOENIX_FIELD_MAP
  │    PER_NUM         -> permit_number
  │    PER_TYPE        -> permit_type
  │    PERMIT_STAT     -> status
  │    PER_ISSUE_DATE  -> issued_date   (epoch ms -> 'YYYY-MM-DD')
  │    PROFESS_NAME    -> general_contractor_name  (via _phoenix_contractor)
  │    STREET_FULL_NAME-> job_address
  ▼
mapped permit dict + raw_source_json = json.dumps(raw)
  │  pipeline/ingestion/upsert.py :: upsert_permit()
  │    key = UNIQUE(jurisdiction, permit_number)
  │    INSERT  -> first_seen_at = last_updated_at = now
  │    UPDATE  -> overwrite all mapped fields INCLUDING raw_source_json,
  │               bump last_updated_at, first_seen_at preserved
  ▼
permits
  │  pipeline/analysis/run_analysis.py :: run_analysis() / run_analysis_for_permits()
  │    normalize_permit_status / normalize_permit_type via knowledge dictionaries
  │    rule-based scoring -> opportunity_score, confidence_score,
  │                          project_category, project_lifecycle,
  │                          estimated_material_value, estimated_gross_profit
  │    DELETE + re-INSERT the projects row (no prior version kept)
  ▼
projects (1:1)  ──►  estimated_materials (regenerated per project)
  │
  │  pipeline/company_resolution/backfill.py
  │    CompanyInput(name=general_contractor_name | plumbing_contractor_name | owner_name)
  │    -> normalize_company_name()
  │    -> match.resolve()  ->  matched | possible_match | new_company
  │         matched        -> link existing companies.id
  │         possible_match -> company_match_review_queue (NOT linked)
  │         new_company    -> INSERT companies + company_aliases + company_roles
  │    writes permits.contractor_company_id  (⚠ bumps permits.last_updated_at)
  │           projects.contractor_company_id / owner_company_id
  │           company_activity rows (dedupe_key-idempotent)
  │           company_identity_audit_log
  ▼
companies ─► company_intelligence (metrics recomputed wholesale)
  │           company_roles, company_aliases
  ▼
pipeline/export/export_json.py  ──► data/exports/*.json (14 files)
pipeline/reports/*              ──► reports/generated/*.md|.xlsx|.pdf
pipeline/crm/service.py         ──► /api/sales/* ──► sales-dashboard.js, company-profile.js
pipeline/crm/admin.py           ──► /api/admin/* ──► admin dashboards
```

The other eight jurisdictions follow the identical path; only the connector builder and
field map differ. `mesa_az` uses `pipeline/connectors/socrata.py` (SoDA API, optional
`SOCRATA_APP_TOKEN` from the environment) instead of the Esri REST path.

---

## 3. Orchestration and scheduling

| Entry point | Trigger | What it runs |
| --- | --- | --- |
| `python -m pipeline.run morning_refresh` | Windows Task Scheduler → `scripts/run_morning_refresh.ps1` | 12-stage orchestrated refresh with locking, retry, freshness and run records (`pipeline_runs`) |
| `automation/run_daily.ps1` | Windows Task Scheduler | `python pipeline/run.py` (all default steps), then an OpenClaw NL summary in WSL |
| `python -m pipeline.run <step>` | Manual / CLI | `ingest`, `analyze`, `rebuild-contractors`, `companies`, `export`, `export-permits`, `reports`, `morning_refresh` |
| `POST /api/admin/morning-refresh/run` | Admin UI button (`pipeline.run` permission) | Same orchestrator, background thread |

Two scheduled paths therefore write to the same database. `pipeline_runs` prevents two
*morning refreshes* from overlapping, but it does **not** prevent `automation/run_daily.ps1`
from running concurrently with a morning refresh, nor either of them from colliding with
the API server's writes. With `journal_mode=delete` this is a real lock-contention surface.

Run history: 533 `ingestion_runs` (508 success, 24 error, 1 success_with_errors) and
4 `pipeline_runs` (3 succeeded, 1 partial).

---

## 4. Normalization, deduplication, resolution, enrichment

| Stage | Implementation | Behaviour |
| --- | --- | --- |
| Field mapping | per-connector `*_FIELD_MAP` dicts | Declarative; pure functions for derived fields |
| Coordinate sanity | `_valid_az_latitude/_valid_az_longitude` | Nulls out-of-range values rather than "correcting" them |
| Date coercion | `_epoch_ms_to_date` | Esri epoch-ms → `YYYY-MM-DD`. **Not applied to Socrata/Mesa**, which keeps ISO timestamps |
| Currency coercion | `_parse_currency_string` | Chandler `$1,234` strings |
| Permit dedup | `UNIQUE(jurisdiction, permit_number)` | Reliable |
| Status/type normalization | `pipeline/knowledge/*` + dictionary tables | Dictionary-driven; unknown values go to `knowledge_review_queue` |
| Company normalization | `company_resolution/normalize.py` | Uppercase, strip punctuation, `&`→`AND`, strip trailing legal suffix, placeholder blocklist |
| Company resolution | `company_resolution/match.py` | Blocking on license/name/alias/phone/source-id; weighted signals; tiered decision |
| Company merge | `company_resolution/merge.py` | Manual, audited, `merged_into_id` tombstone |
| Enrichment | none | No third-party firmographic, licensing-board, parcel, or contact enrichment exists |

### Entity-resolution decision tiers (as implemented)

`resolve()` returns `matched` / `possible_match` / `new_company` based on
`COMPANY_MATCH_AUTO_MIN`, `COMPANY_MATCH_PROBABLE_MIN`, `COMPANY_MATCH_REVIEW_MIN` in
`settings.py`. Signals: `source_id_exact`, `name_exact`, `alias_exact`,
`name_plus_phone`, `name_plus_city`, `license_exact`, `phone_exact`, `email_domain`.
Conflicting fields (`license_number`, `city`, `state`, `main_phone`) demote an otherwise
probable match into the review queue. License number is deliberately corroborating-only.

This design is sound. Its problem is not logic but **evidence**: see §6.

---

## 5. Where lineage is lost today

This is the crux of the RAW-layer argument.

| Loss point | Detail | Consequence |
| --- | --- | --- |
| **Raw payload not persisted independently** | The source dict exists only in memory; `raw_source_json` is stored on the permit row and **overwritten on every update** | Cannot replay, cannot re-map a field retroactively, cannot audit what the source actually said last month |
| **No batch/run id on the record** | `ingestion_runs` exists but no permit row references a run | Cannot answer "which run produced or changed this row" |
| **No source URL on the record** | Endpoint is known only at the jurisdiction level | Cannot cite a per-record provenance in a licensed dataset |
| **No checksum** | No payload hash | Cannot cheaply detect real vs no-op change; drives the churn documented in `01` |
| **No `source_updated_at`** | Source-side modification time is never captured, even where available | Cannot distinguish "the city changed this" from "we re-ran the pipeline" |
| **Company lineage is a stub** | `companies.source_system = 'permits'` for all 12,636 rows; `source_record_id` unpopulated | Cannot trace a canonical company back to the specific permits that created it, except via `company_activity` |
| **No `source_record_map`** | No table binding (source_system, source_record_id) → canonical entity with match method and confidence | Entity-resolution decisions are logged in `company_identity_audit_log` but not queryable as current state |
| **Relationship confidence unpopulated** | `company_roles.confidence` NULL on all 12,794 rows | Cannot distinguish a verified contractor-of-record from a heuristic string match |
| **Inferred vs verified indistinguishable** | Phoenix `_phoenix_contractor` falls back to `PERMIT_NAME` when it merely *looks like* a company; Tempe does the same with `ProjectName` | Heuristic attributions are stored identically to authoritative ones |

---

## 6. Proposed lineage model

The target lineage spine, to be introduced additively (detailed DDL in `05_PROPOSED_SCHEMA.md`):

```
raw_ingest_batch
    batch_id, run_id, source_system, jurisdiction_slug, source_url,
    connector_type, requested_since, started_at, completed_at,
    status, records_fetched

raw_record
    raw_record_id, batch_id, source_system, source_entity_type,
    source_record_id, payload_json, payload_hash, source_url,
    source_updated_at, fetched_at, is_current

source_record_map
    source_system, source_record_id, raw_record_id,
    canonical_entity_type, canonical_entity_id,
    relationship_type, match_method, match_confidence,
    first_seen, last_seen
```

Every curated entity then answers "where did this come from" with a single join, and
every attribution carries a `match_method` (`source_id_exact`, `name_exact`,
`name_plus_city`, `heuristic_projectname`, …) and a `match_confidence`. Heuristic
attributions such as the Phoenix/Tempe project-name fallback become explicitly labelled
rather than silently equivalent to authoritative contractor fields.

---

## 7. Licensing and attribution obligations

All nine connected sources are municipal open-data endpoints. Internal analytical use is
low-risk; **redistribution as a licensed dataset is not, and has not been assessed.**

Before any CorridorIQ DATA product ships, each source needs a recorded decision on:

- terms of use / open-data licence and its attribution clause
- whether redistribution or resale is permitted
- whether derived-work distribution is permitted
- required attribution string and where it must appear
- rate-limit and caching obligations

Two specific flags already visible in the codebase:

- `jurisdictions.yaml` notes Accela Citizen Access scraping "may raise ToS concerns" for
  Scottsdale — the connected Scottsdale feed is the separate open-data table, not Accela,
  and that distinction must be preserved in any future connector work.
- `SOCRATA_APP_TOKEN` is optional; unauthenticated Socrata use is throttled and is subject
  to Mesa's terms.

Recommendation: add `license_code`, `license_url`, `attribution_text`,
`redistribution_allowed` and `terms_reviewed_at` columns to the jurisdiction/source
registry, and treat `redistribution_allowed IS NOT 1` as a hard filter in any export or
API destined for a customer.
