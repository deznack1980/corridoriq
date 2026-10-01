# 03 — Data Quality Risks

**Audit date:** 2026-09-15
**Population:** 110,298 permits · 110,298 projects · 12,636 companies ·
175,827 activity events · 334,076 material estimates

All figures below were produced by non-destructive `SELECT` queries against a read-only
connection to the live database. Percentages are of the relevant population.

---

## Risk register (ranked by commercial impact)

| # | Risk | Measure | Severity |
| --- | --- | --- | --- |
| 1 | No historical retention | 47.8% of permits overwritten; 100% of scores dated one day | **Critical** |
| 2 | No true project entity | 57.6% of permits share an address with another permit | **Critical** |
| 3 | Valuation unusable | 73.9% of permits have null or zero valuation | **High** |
| 4 | Contractor attribution missing | 34.2% of permits have no company link | **High** |
| 5 | Vocabulary fragmentation | 419 permit types / 58 statuses vs 24 / 17 dictionary entries | **High** |
| 6 | Company records are name-only shells | 100% missing address, phone, email, website | **High** |
| 7 | Update churn corrupts change signals | 12,250 "updates" from 245 real Goodyear records | **Medium-High** |
| 8 | Duplicate entity representations | `contractors` vs `companies`; `municipalities` vs `jurisdictions` | **Medium** |
| 9 | Location data sparse | 85% no zip, 71% no coordinates, 100% no APN | **Medium** |
| 10 | Mixed date formats | 10,485 rows non-ISO | **Medium** |
| 11 | Relationship confidence unpopulated | 12,794 of 12,794 `company_roles.confidence` NULL | **Medium** |
| 12 | Coverage bias by jurisdiction | Phoenix = 44.9% of all permits, with zero valuation | **Medium** |
| 13 | Impossible date sequences | 8 filed-after-issued, 57 finaled-before-issued | **Low** |
| 14 | Unused / stub tables | `contacts`, `quotes`, `deliveries`, `pricing_config` all empty | **Low** |

---

## 1. No historical retention — **Critical**

| Metric | Value |
| --- | --- |
| Permits with `last_updated_at <> first_seen_at` | 52,755 (47.8%) |
| Prior values retained for those overwrites | 0 |
| Distinct `projects.analyzed_at` days | **1** (`2026-09-15`) |
| Distinct `projects.analysis_version` | 1 |
| Tables with `source_updated_at` | 0 |
| Distinct `permits.first_seen_at` days | 25 |

Nearly half the permit corpus has been silently rewritten. Every scoring output in the
business is a single-day snapshot. The historical asset that CorridorIQ DATA is supposed
to monetise does not currently exist — it is being destroyed on every pipeline run.

This is not recoverable retroactively. Value accrues only from the date a RAW layer is
turned on, which is the strongest possible argument for doing it first.

## 2. No true project entity — **Critical**

| Metric | Value |
| --- | --- |
| Permits | 110,298 |
| Distinct non-empty `job_address` | 48,103 |
| Addresses with more than one permit | 16,644 |
| Permits sharing an address | 63,570 (57.6%) |
| `projects` rows | 110,298 (`UNIQUE(permit_id)`) |

Worst offenders: `32200 N 43RD AVE` (634 permits), `5088 W INNOVATION CIR` (410),
`2800 W SONORAN DESERT DR` (293), `8200 W VIRGINIA AVE` (208), `201 E JEFFERSON ST` (207).

Each of these is one real development represented as hundreds of independent "projects",
each scored separately. Consequences: opportunity scores are computed per-permit rather
than per-job, so a large development is invisible as a large development; contractor
activity counts are inflated by permit volume rather than job count; "project momentum"
and "project duration" cannot be derived at all.

## 3. Valuation is unusable for most of the corpus — **High**

| Metric | Count | % |
| --- | ---: | ---: |
| `valuation IS NULL` | 58,807 | 53.3% |
| `valuation = 0` | 22,685 | 20.6% |
| **Null or zero** | **81,492** | **73.9%** |
| Negative or > $1B (absurd) | 0 | 0% |

By jurisdiction:

| Jurisdiction | Permits | Null valuation | Zero valuation |
| --- | ---: | ---: | ---: |
| `phoenix_az` | 49,510 | **49,510 (100%)** | 0 |
| `peoria_az` | 3,335 | **3,335 (100%)** | 0 |
| `mesa_az` | 10,485 | 5,856 | 1,046 |
| `scottsdale_az` | 15,484 | 9 | 10,618 |
| `gilbert_az` | 12,101 | 0 | 8,408 |
| `buckeye_az` | 13,267 | 0 | 1,482 |
| `tempe_az` | 5,786 | 93 | 1,127 |

Phoenix and Peoria have no valuation field at the source at all (documented in the
connector). Since Phoenix alone is 44.9% of the corpus, any valuation-weighted metric is
structurally biased toward the smaller cities that happen to publish the field. The
absence is honest — the risk is that downstream aggregates treat it as $0 rather than
unknown.

## 4. Contractor attribution missing — **High**

| Metric | Count | % |
| --- | ---: | ---: |
| No contractor name of any kind | 35,560 | 32.2% |
| No `contractor_company_id` link | 37,713 | 34.2% |
| No `contractor_license_number` | 109,750 | 99.5% |
| Projects with any company link | 72,876 | 66.1% |

By jurisdiction:

| Jurisdiction | Permits | No contractor name | No company link |
| --- | ---: | ---: | ---: |
| `buckeye_az` | 13,267 | 13,267 (100%) | 13,267 |
| `gilbert_az` | 12,101 | 12,101 (100%) | 12,101 |
| `chandler_az` | 85 | 85 (100%) | 85 |
| `tempe_az` | 5,786 | 4,996 (86%) | 5,010 |
| `phoenix_az` | 49,510 | 4,232 (9%) | 5,847 |
| `scottsdale_az` | 15,484 | 751 (5%) | 1,238 |
| `mesa_az` | 10,485 | 31 (0.3%) | 31 |

Buckeye, Gilbert and Chandler (25,453 permits, 23% of the corpus) have **no contractor
field in the source**. This is a source gap, not a pipeline bug, and it cannot be closed
by better matching — only by a second source (e.g. the AZ ROC licensing board) or by
parcel/address-based inference with explicit confidence.

The 99.5% missing license number is the reason company resolution has so little
corroborating evidence to work with.

## 5. Vocabulary fragmentation — **High**

| Dimension | Distinct raw values | Dictionary entries | Pending review |
| --- | ---: | ---: | ---: |
| `status` | 58 | 17 (`status_dictionary`) | 38 |
| `permit_type` | 419 | 24 (`permit_code_dictionary`) | 382 |
| `permit_subtype` | 395 | — | — |
| keywords | — | 62 (`keyword_dictionary`) | — |

The same real-world state appears as `DONE` (29,720), `Finaled` (15,162), `FINALLED`
(8,961), `Final` (4,187), `Complete` (128) — five spellings across jurisdictions.
Similarly `C of C Issued` / `C of O Issued` / `CofO Issued`. Only `Finaled` (4
jurisdictions), `Issued` (4) and `Expired` (5) span more than a couple of cities.

Phoenix alone contributes 215 distinct permit types. The `knowledge_review_queue` holds
**420 pending** unmapped values (382 permit codes, 38 statuses) — a real, growing
normalization backlog that directly degrades `project_category` (60,359 projects, 54.7%,
fall into the catch-all `Other`).

## 6. Company records are name-only shells — **High**

| Field | Missing | % |
| --- | ---: | ---: |
| `address_line_1` | 12,636 | **100%** |
| `main_phone` | 12,636 | **100%** |
| `main_email` | 12,636 | **100%** |
| `website` | 12,636 | **100%** |
| `license_number` | 12,420 | 98.3% |
| `contacts` table | 0 rows | — |

Two consequences. Commercially, there is no way to contact a prioritised lead from within
CorridorIQ — the CRM has nothing to dial. Architecturally, entity resolution is running on
a single signal (normalized name), because every corroborating field it is designed to use
(phone, email domain, address, license) is empty. The matcher reports 0 duplicate
normalized names, but that is a tautology: name *is* the identity, so duplicates are
definitionally impossible while genuine variants of the same firm remain separate rows.

`company_aliases` has 12,636 rows and **0** whose normalized alias differs from the parent
company's normalized name — the alias table carries zero information today.

## 7. Update churn corrupts change signals — **Medium-High**

Cumulative `ingestion_runs` totals:

| Jurisdiction | Real records | Recorded updates | Runs | Inflation |
| --- | ---: | ---: | ---: | --- |
| `goodyear_az` | 245 | 12,250 | 54 | every record, every run |
| `chandler_az` | 85 | 8,755 | 59 | every record, every run |
| `phoenix_az` | 49,510 | 398 | 53 | healthy |
| `scottsdale_az` | 15,484 | 0 | 64 | healthy |

Goodyear and Chandler have no working incremental date filter, so each run re-fetches
everything and issues a blind `UPDATE`, bumping `last_updated_at` on rows that did not
change.

Separately, on the audit date only **486 records** were fetched from all sources, yet
**8,140 permits** carry `last_updated_at = today`. The excess comes from internal company
backfill writing `contractor_company_id` to `permits`, which bumps the same column. Source
change time and internal processing time are conflated.

Net effect: `last_updated_at` cannot be used to answer "what changed", to drive
incremental downstream processing, or to build a change feed for customers. A payload hash
in a RAW layer solves all three.

## 8. Duplicate entity representations — **Medium**

| Metric | Value |
| --- | --- |
| `contractors` rows | 11,034 |
| …matching a `companies.normalized_name` | 10,286 |
| …with **no** matching company | **748** |
| `jurisdictions` rows | 13 |
| `municipalities` rows | 16 |

`contractors` is the pre-`companies` entity, still rebuilt by
`pipeline/contractors/rebuild.py`, still joined by `projects.contractor_id`, and still
exported to `data/exports/contractors.json`. 748 contractor records have drifted out of
alignment with the canonical company registry. Two registries of the same real-world
entity will diverge further with every run.

`municipalities` and `jurisdictions` are likewise two registries of the same concept,
with different keys and different freshness columns.

## 9. Location data sparse — **Medium**

| Field | Missing | % |
| --- | ---: | ---: |
| `apn` | 110,298 | **100%** |
| `zip` | 93,696 | 85.0% |
| `latitude`/`longitude` | 78,522 | 71.2% |
| `parcel_number` | 57,101 | 51.8% |
| `job_address` | 15,269 | 13.8% |
| `city` | 0 | 0% (back-filled from connector `city_name`) |

Distinct parcel numbers: 32,424. With 100% missing APN and 52% missing parcel, neither
field can serve as the join key for address-based project grouping on its own — grouping
will need normalized address plus parcel where available, with an explicit confidence
tier. Only 28.8% of permits are geocodable today, which caps every geographic product.

## 10. Mixed date formats — **Medium**

| Format | Rows | Source |
| --- | ---: | --- |
| `YYYY-MM-DD` (length 10) | 99,813 | all ArcGIS connectors (`_epoch_ms_to_date`) |
| `YYYY-MM-DDTHH:MM:SS.sss` (length 23) | 10,485 | `mesa_az` (Socrata, passed through) |

Because SQLite stores dates as text and the codebase compares them as strings, 9.5% of
rows sort and filter differently from the rest. Most queries defensively use
`substr(col,1,10)`, but that is a convention, not a constraint — any query that forgets it
silently excludes Mesa.

## 11. Relationship confidence unpopulated — **Medium**

| Metric | Value |
| --- | --- |
| `company_roles` rows | 12,794 |
| …with non-NULL `confidence` | **0** |
| …with non-NULL `effective_from`/`effective_to` | **0** |
| `company_match_review_queue` pending | 43 (avg confidence 86.0) |

The schema is designed for confidence-scored, time-bounded roles. Neither is populated.
Additionally `company_roles` is `UNIQUE(company_id, role_type)` — a global role, not a
per-project one, so "was this firm the GC on *this* job" is not representable.

Distinct `(company_id, project_id)` pairs in `company_activity`: **76,415**. That is the
real relationship set, and it carries no confidence or role-verification field either.

Inferred-vs-verified is also invisible at the source-mapping layer: the Phoenix connector
falls back to `PERMIT_NAME` when it merely *looks like* a company
(`_looks_like_company()` keyword heuristic), and Tempe does the same with `ProjectName`.
Those heuristic attributions are stored identically to Mesa's authoritative
`contractor_name`.

## 12. Coverage bias by jurisdiction — **Medium**

| Jurisdiction | Permits | Share |
| --- | ---: | ---: |
| `phoenix_az` | 49,510 | 44.9% |
| `scottsdale_az` | 15,484 | 14.0% |
| `buckeye_az` | 13,267 | 12.0% |
| `gilbert_az` | 12,101 | 11.0% |
| `mesa_az` | 10,485 | 9.5% |
| `tempe_az` | 5,786 | 5.2% |
| `peoria_az` | 3,335 | 3.0% |
| `goodyear_az` | 245 | 0.2% |
| `chandler_az` | 85 | 0.1% |

Nearly half the corpus comes from the one source with no valuation. Goodyear and Chandler
feeds are narrow by scope (Accela construction/TI only, Chandler BLD "Under Construction"
only) and Chandler's endpoint has been returning Esri 400 errors. Four Phoenix-metro
cities remain unconnected entirely. Any "market activity" product must publish coverage
metadata or it will mislead.

Temporal coverage is also thin: issued dates concentrate in 2024 (26,257), 2025 (58,149)
and 2026 (25,767), with only 125 rows before 2024. CorridorIQ effectively holds ~2.5 years
of history.

## 13. Impossible date sequences — **Low**

| Check | Rows |
| --- | ---: |
| `filed_date > issued_date` | 8 |
| `finaled_date < issued_date` | 57 |
| `issued_date` in the future | 0 |
| `issued_date` before 1990 | 0 |

Small absolute numbers, but there is no `CHECK` constraint or validation stage that would
catch a larger regression.

## 14. Unused / stub tables — **Low**

`contacts` (0), `quotes` (0), `deliveries` (0), `pricing_config` (0),
`crm_activity_revisions` (0). `suppliers` has 1 row. These are scaffolding for features
not yet in use; harmless, but they should be marked as such so the target schema does not
inherit them by default.

---

## What is clean

For balance, these checks came back perfect and should not be re-litigated:

- `PRAGMA foreign_key_check` across all 43 tables: **0 violations**
- Orphan checks on `projects→permits`, `company_activity→companies`,
  `company_activity→projects`, `estimated_materials→projects`,
  `permits.contractor_company_id→companies`, `companies.merged_into_id`: **all 0**
- Duplicate `companies.normalized_name` groups: **0**
- Permits with missing `permit_number`: **0**
- Permits with neither `filed_date` nor `issued_date`: **0**
- Negative or absurd valuations: **0**
- Company merges performed without an audit-log entry: none (12,637 log rows)

The integrity discipline in this codebase is good. The gaps are about *what is recorded*,
not about whether what is recorded is internally consistent.
