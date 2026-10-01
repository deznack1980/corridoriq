# 04 — Target Architecture

**Status:** design proposal. Nothing in this document has been implemented.

---

## 1. Design principles

1. **Additive only.** RAW and CURATED are new tables alongside the existing ones. No
   production table is renamed, dropped or restructured. The application keeps reading
   `permits`, `projects`, `companies` until it is explicitly repointed.
2. **Same stack first.** SQLite, Python stdlib, hand-written SQL. No new services, no
   queue, no Spark, no dbt, no Airflow. The current data volume (813k rows, 283 MB) does
   not justify any of them, and introducing them would stall the migration.
3. **Capture before modelling.** The RAW layer is worth more than a perfect schema,
   because RAW can be re-modelled later and lost history cannot be recovered. Turn on
   capture first, refine entities second.
4. **Never destroy evidence.** RAW is append-only. CURATED is derivable from RAW. GOLD is
   derivable from CURATED. If a transformation is wrong, rebuild the layer below it.
5. **Confidence is a first-class column.** Any inferred fact — a company match, a project
   grouping, a role attribution — carries a method and a confidence, and uncertain facts
   are never silently merged into certain ones.
6. **Preserve what works.** Deterministic matching philosophy, natural keys, connector
   honesty, the 60-point scoring threshold and its weights all carry forward unchanged.

---

## 2. Layer overview

```
        SOURCES                RAW                    CURATED                  GOLD
   ┌──────────────┐     ┌────────────────┐     ┌──────────────────┐   ┌────────────────────┐
   │ 9 municipal  │     │ raw_ingest_    │     │ curated_permit   │   │ gold_project_      │
   │ permit APIs  │────►│   batch        │────►│ curated_project  │──►│   activity         │
   │ (ArcGIS,     │     │ raw_record     │     │ curated_company  │   │ gold_company_      │
   │  Socrata)    │     │  (append-only, │     │ curated_location │   │   profile          │
   ├──────────────┤     │   hashed,      │     │ curated_parcel   │   │ gold_company_      │
   │ supplier CSV │────►│   immutable)   │     │ curated_contact  │   │   activity_monthly │
   ├──────────────┤     │                │     │ link + alias     │   │ gold_market_       │
   │ future: ROC, │     │                │     │   tables         │   │   activity         │
   │ parcel, firmo│     │                │     │ *_change_event   │   │ gold_opportunity   │
   └──────────────┘     └────────────────┘     │ source_record_map│   │ gold_supplier_     │
                                               └──────────────────┘   │   demand           │
                                                                      └────────────────────┘
                                                        │                        │
                                                        ▼                        ▼
                                              CorridorIQ Intelligence    CorridorIQ DATA
                                              (app, CRM, dashboards)     (API, feeds, exports)
```

| Layer | Mutability | Grain | Rebuildable | Owner |
| --- | --- | --- | --- | --- |
| RAW | append-only, never updated | one row per observed version of a source record | no — it *is* the record | ingestion |
| CURATED | upsert + change events | one row per canonical real-world entity | yes, from RAW | data platform |
| GOLD | full or incremental rebuild | one row per analytical fact | yes, from CURATED | product |

---

## 3. RAW layer

**Purpose:** never lose a source observation again.

### Contract

- Nothing is ever updated or deleted in RAW.
- A new `raw_record` row is written **only when the payload hash differs** from the most
  recent row for the same `(source_system, source_record_id)`. Identical re-fetches
  increment a counter on the batch instead of writing a row. This directly kills the
  Goodyear/Chandler churn documented in `03`, and it makes "what actually changed today"
  a trivial query.
- Every row carries: source system, source record id, full payload JSON, payload hash,
  source URL, source-side updated timestamp (where the source exposes one), our fetch
  timestamp, and the batch id.
- `is_current` is maintained as a convenience flag so "latest version" needs no window
  function; it is the only field ever rewritten, and only from 1 to 0.

### Why this is the first thing to build

The valuation history, status history and contractor-attribution history that CorridorIQ
DATA is meant to sell start accruing on the day RAW is switched on, and not one day
earlier. Every week of delay is a permanent hole in the asset. RAW can be dual-written
from the existing `upsert_permit()` call site in roughly one file's worth of change, with
zero effect on application behaviour.

### Growth estimate

Current `raw_source_json` averages roughly 1.5–2.5 KB per permit. At 110k permits the
initial RAW snapshot is ~200–275 MB. Realistic change volume — once no-op churn is
eliminated by hashing — is on the order of 40–60k genuine versions per year, i.e.
**~100–150 MB/year**. SQLite handles this comfortably for several years. See §8 for the
trigger points that would justify moving RAW to Postgres or to partitioned Parquet.

---

## 4. CURATED layer

**Purpose:** one clean, canonical, traceable, historised entity per real-world thing.

### Entities

| Entity | Grain | Key new capability vs today |
| --- | --- | --- |
| `curated_jurisdiction` | one per source jurisdiction | merges `jurisdictions` + `municipalities`; adds licence/attribution fields |
| `curated_location` | one per normalized address | **new** — addresses become an entity, enabling project grouping and geo products |
| `curated_parcel` | one per APN/parcel id | **new** |
| `curated_permit` | one per `(jurisdiction, permit_number)` | adds `source_updated_at`, `raw_record_id`, `first_seen`/`last_seen` that mean what they say |
| `curated_project` | one per **real job** | **new** — breaks the 1:1 permit tie; multiple permits per project |
| `curated_company` | one per canonical firm | adds populated lineage and resolution tier |
| `curated_company_alias` | one per observed name variant | finally carries real variants |
| `curated_contact` | one per person | scaffolding for future enrichment |
| `curated_project_permit` | link | which permits constitute which job, with grouping confidence |
| `curated_project_company` | link | **per-project** role with confidence and method (today roles are global) |
| `source_record_map` | lineage spine | every canonical id ↔ every source record that evidences it |

### The project entity

This is the highest-value modelling change. `curated_project` groups permits by:

1. `parcel_number` where present (32,424 distinct values available), else
2. normalized `job_address` + jurisdiction (48,103 distinct values), plus
3. a time-proximity window, so a 2019 remodel and a 2026 remodel at the same address are
   separate jobs rather than one 7-year project.

Each grouping records `grouping_method` and `grouping_confidence`, and single-permit jobs
are simply projects of size one. Expected effect: ~110k permit rows collapse toward
roughly 48k–60k real projects, with 16,644 addresses becoming genuinely multi-permit jobs.

**Critically, this does not disturb scoring.** The existing `projects` table and its
`opportunity_score` / 60-point threshold stay exactly as they are and keep driving the
application. `curated_project` is a parallel, additive grouping. Rolling scoring up to
job level is a later, separate, explicitly-approved decision.

### History strategy

Three mechanisms, each chosen for the smallest thing that answers a real question:

| Mechanism | Applies to | Answers |
| --- | --- | --- |
| **Append-only RAW versions** (hash-based) | every source record | "what did the source say on date X" — full fidelity, zero modelling cost |
| **Narrow change-event tables** | a short list of high-value attributes | "when did status/valuation/lifecycle/contractor change" — cheap, queryable, indexable |
| **`first_seen` / `last_seen` on curated rows and links** | every entity and relationship | "when did we first/last observe this" — including relationship history |

Explicitly **rejected**: full Type-2 slowly-changing dimensions on every curated table.
With 36 columns on permits and 110k rows, SCD2 everywhere would multiply storage and make
every application query need an `AND is_current = 1` predicate — a large blast radius for
questions nobody is asking. RAW already provides full-fidelity history; change events
provide the fast path for the handful of attributes that matter commercially.

Attributes proposed for change-event tracking:

- permit: `status`, `normalized_status`, `valuation`, `issued_date`, `finaled_date`,
  `contractor_company_id`
- project: `lifecycle`, `opportunity_score`, `estimated_material_value`
- company: `lifecycle_state`, `merged_into_id`, `license_status`
- relationship: any project↔company link created, confirmed or withdrawn

### Provenance

Every curated row carries `raw_record_id` (the version it was built from) and is joined to
`source_record_map` for the full set of source records that evidence it. A curated company
built from 40 permits has 40 `source_record_map` rows, each with its own `match_method`
and `match_confidence`.

---

## 5. GOLD layer

**Purpose:** the things customers actually buy. Rebuildable, denormalised, fast.

| Dataset | Grain | Feeds |
| --- | --- | --- |
| `gold_project_activity` | project | project discovery, active-project feed, momentum |
| `gold_high_value_projects` | project | lead prioritisation |
| `gold_company_profile` | company | contractor intelligence, company pages |
| `gold_company_activity_monthly` | company × month | growth trends, "who is accelerating" |
| `gold_contractor_project_graph` | company × project | relationship graph, network products |
| `gold_market_activity` | jurisdiction × month × category | market intelligence, geographic demand |
| `gold_opportunity` | project | **existing scoring output, unchanged weights/threshold** |
| `gold_supplier_demand` | material × geography × month | supplier intelligence, material demand |

GOLD is where `company_intelligence`, `contractors` and the `data/exports/*.json` files
eventually land as properly-derived marts rather than in-place-mutated tables. Each GOLD
table gets a `generated_at` and `source_watermark` so a consumer knows exactly how fresh
it is and which CURATED state produced it.

Start with two: `gold_company_profile` (replaces `company_intelligence` semantics) and
`gold_project_activity`. Build the rest on demand. Do not pre-build eight marts for
customers who do not exist yet.

---

## 6. Entity resolution

Keep the existing deterministic, tiered, conservative matcher. Extend it, do not replace
it.

### Tier model

| Tier | Trigger | Action |
| --- | --- | --- |
| `EXACT` | source-system id match, or normalized name + license match | auto-link |
| `HIGH_CONFIDENCE` | normalized name + (phone \| city \| domain), no conflicting fields | auto-link |
| `PROBABLE` | single strong signal, or a match with a conflicting field | **review queue, never auto-merged** |
| `UNRESOLVED` | below review threshold, or name is a placeholder | new canonical entity, flagged |

This maps directly onto the existing `DECISION_MATCHED` / `DECISION_POSSIBLE` /
`DECISION_NEW` outcomes and `COMPANY_MATCH_AUTO_MIN` / `_PROBABLE_MIN` / `_REVIEW_MIN`
thresholds. The change is that the tier, method and confidence become **persisted state**
in `source_record_map` rather than a transient decision that only survives in the audit
log.

### Signal roadmap

Today only one signal is populated (normalized name), because 100% of companies lack
phone, email, address and 98% lack a license number. Priority order for making resolution
actually work:

1. **Persist per-source evidence** — `source_record_map` gives every company the set of
   permits, jurisdictions and dates that evidence it. Co-occurrence alone is a real signal.
2. **AZ ROC licence registry** — the highest-leverage external source. Supplies legal
   name, DBA, licence number, status, address and classification, and would lift licence
   coverage from 1.7% toward near-complete for licensed contractors.
3. **Parcel/assessor data** — resolves `owner_name` (15,722 permits, 10,826 distinct
   values) into real owner entities and separates individuals from LLCs.
4. **Domain/firmographic enrichment** — last, and only for companies that already matter
   commercially.

### Guardrails carried forward

- Licence number remains **corroborating-only**, never a standalone auto-match. The
  existing code comment about shared placeholder licences like `HTE0001` documents a real
  hazard and should stay.
- The placeholder blocklist (`OWNER`, `TBD`, `TO BE BID`, …) stays and should be extended
  as new placeholders are observed.
- Heuristic attributions — the Phoenix `PERMIT_NAME` fallback and the Tempe `ProjectName`
  fallback — get an explicit `match_method` such as `heuristic_project_name` and a
  materially lower confidence, so downstream products can filter them out. Today they are
  indistinguishable from Mesa's authoritative contractor field.
- Merges remain manual and audited.

---

## 7. Data product readiness

What must be true before `GET /companies` can be offered externally. None of this requires
building the API now.

| Requirement | Why | Status |
| --- | --- | --- |
| **Stable public IDs** | Sequential SQLite rowids leak volume and break if a table is rebuilt. Customers cache IDs forever. | Missing — needs an opaque, immutable `public_id` (ULID/UUID) per curated entity |
| **As-of queries** | The differentiator for a data product is "what did this look like in March". | Missing — delivered by RAW + change events |
| **Change feed** | Enterprise feeds are consumed incrementally, not re-downloaded. | Missing — needs a trustworthy `updated_at` watermark, which today's churny `last_updated_at` cannot provide |
| **Cursor pagination** | Offset pagination breaks under concurrent writes. | Missing |
| **Field-level provenance** | A buyer will ask "where did this contractor attribution come from". | Missing — `source_record_map` provides it |
| **Confidence exposure** | Inferred relationships must be labelled, or the product is misleading. | Missing |
| **Coverage metadata** | 4 of 13 jurisdictions unconnected; Phoenix has no valuation. A market-activity product that hides this is wrong. | Missing — needs per-source coverage/freshness published alongside data |
| **PII suppression** | `owner_name` includes individuals; 1,776 companies are typed `property_owner`. | Missing — needs a redaction/entitlement layer |
| **Redistribution rights per source** | Municipal open data varies; not yet assessed. | Missing — see `02` §7 |
| **Rate limiting + entitlements** | Distinct from the internal session RBAC that exists today. | Missing |
| **Published data dictionary + SLA** | Contractual surface. | Missing |
| **Read isolation** | An external API must not contend with the ingestion writer. | Blocked by single-file SQLite in rollback journal mode |

The read-isolation row is the one that eventually forces a database decision.

---

## 8. Technology trajectory

**Now:** stay on SQLite. It is not the bottleneck at 283 MB, and switching engines
mid-migration would guarantee the migration does not finish.

**Immediately advisable regardless of layer work** (these are operational, not
architectural):

- move the database file **out of the OneDrive-synced tree**
- switch `journal_mode` to **WAL** so readers stop blocking on the writer
- guarantee `foreign_keys` on every connection (SQLite has no persistent
  file-level setting; the pragma resets to OFF on each new connection)
- stamp `user_version` so schema state is inspectable
- add a real backup: a scheduled `VACUUM INTO` snapshot, retained and rotated

**Migrate to PostgreSQL when any two of these become true:**

- more than one concurrent writer is genuinely needed (external API + ingestion + CRM)
- the database exceeds roughly 10–20 GB, which RAW growth reaches in ~5–8 years at
  current source volume, or much sooner if jurisdictions expand beyond Arizona
- customers require concurrent read access with latency guarantees
- the team needs real migrations, row-level security, or PostGIS for geographic products
- analytical queries over GOLD start taking longer than a few seconds

The layered design is deliberately portable: RAW is JSON blobs plus scalars, CURATED is
plain relational, GOLD is derived. A future port is a schema translation and a bulk copy,
not a redesign. Geographic products in particular will eventually want PostGIS — worth
noting that only 28.8% of permits are geocodable today, so that is not the near-term
constraint.

**Not recommended at this scale:** dbt, Airflow, Spark, a separate warehouse, a message
queue, or a lakehouse. Revisit when GOLD rebuild time exceeds the morning-refresh window
or when more than one engineer is writing transformations concurrently.
