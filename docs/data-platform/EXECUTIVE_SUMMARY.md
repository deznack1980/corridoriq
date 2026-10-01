# Executive Summary — CorridorIQ Data Platform Audit

**Date:** 2026-09-15 · **Phase:** audit and design only · **Nothing was changed**

---

## The one-paragraph version

CorridorIQ has built a genuinely good permit pipeline and a clean, well-disciplined
database — 110,298 permits across nine Arizona jurisdictions, 12,636 companies, zero
referential-integrity violations. But it is storing **only the present**. Nearly half the
permit records have been overwritten with no prior values kept, and every opportunity
score in the business is dated a single day, because each analysis run replaces all of
them. The historical data asset that CorridorIQ DATA is meant to monetise does not exist
yet and is being destroyed a little more on every run. Separately, "project" is not
actually modelled — it is one row per permit, while 58% of permits share an address with
another permit, so large developments appear as hundreds of unrelated jobs. Both problems
are fixable additively, without touching the working application. The first fix is worth
starting immediately because history cannot be backfilled.

---

## 1. What CorridorIQ has today

One SQLite database, `pipeline/db/corridoriq.db`, 283 MB, 43 tables, 813,467 rows.

| | |
| --- | --- |
| Permits | 110,298 across 9 connected jurisdictions (4 more identified, not connected) |
| Projects | 110,298 — exactly 1:1 with permits |
| Companies | 12,636, plus 11,034 rows in a legacy parallel `contractors` table |
| Relationships | 76,415 distinct company↔project pairs, via 175,827 activity events |
| Material estimates | 334,076 |
| Temporal coverage | ~2.5 years; only 125 permits pre-date 2024 |

Ingestion runs from nine live municipal endpoints (eight ArcGIS/Esri, one Socrata) on two
Windows Task Scheduler jobs. Data flows permits → scoring → company resolution → JSON
exports and reports → an authenticated internal API → HTML dashboards. There is no
staging layer, no warehouse, no public API.

## 2. What is strong

- **Honest connectors.** They raise errors rather than returning fabricated or empty data,
  and every source gap is documented per jurisdiction. This is rarer than it sounds and is
  the foundation everything else rests on.
- **Clean integrity.** Zero foreign-key violations and zero orphans across 813k rows.
  Real natural keys (`UNIQUE(jurisdiction, permit_number)`) make ingestion genuinely
  idempotent.
- **Conservative entity resolution.** Deterministic, tiered, never auto-merges uncertain
  matches, and deliberately treats licence numbers as corroborating-only because the
  source data contains shared placeholder licences. The philosophy is right; it should be
  extended, not replaced.
- **Operational maturity already present.** Run tracking, audit logs, RBAC, tenancy,
  retry and failure isolation, and a 149-test suite.

## 3. What is fragile

- **The database file lives in a OneDrive-synced folder**, in rollback journal mode, with
  a scheduled writer and a long-running server. This is the single largest operational
  risk and it has nothing to do with schema design.
- **`last_updated_at` cannot be trusted.** Goodyear's 245 real records have produced
  12,250 recorded "updates" across 54 runs because the connector has no incremental
  filter. Separately, only 486 records were fetched on the audit date but 8,140 permits
  show today's timestamp, because internal company backfill bumps the same column. Source
  change time and processing time are conflated.
- **Two entities are represented twice** — `contractors` vs `companies` (748 contractor
  rows have already drifted with no matching company), and `municipalities` vs
  `jurisdictions`.
- **Company records are name-only shells.** 100% are missing address, phone, email and
  website; 98% lack a licence number. Entity resolution is therefore running on a single
  signal, which makes today's "zero duplicate companies" a tautology rather than a result.
- **Vocabulary is fragmenting faster than it is being curated.** 419 distinct permit types
  and 58 statuses against dictionaries holding 24 and 17, with 420 items backed up in the
  review queue. 54.7% of projects fall into the catch-all `Other` category.
- **Every consumer uses hardcoded raw SQL.** No repository layer, no views. 38 modules
  reference core tables directly; `crm/service.py` alone has 40 such statements.

## 4. What threatens scalability

Not row count. 813k rows in SQLite is comfortable, and the honest answer is that the
database engine is nowhere near being the bottleneck.

The real constraints, in order:

1. **Concurrency.** Rollback journal mode plus two schedulers plus an API server means
   writers block readers. `pipeline_runs` prevents two morning refreshes overlapping but
   does not prevent `run_daily.ps1` colliding with one. An external API cannot be added on
   top of this.
2. **Full recomputation.** Every analysis pass rewrites all 110,298 project rows. This
   works at 110k and will not at 1M. The morning refresh added incremental paths; the
   general path still does not use them.
3. **Coverage expansion.** Nine jurisdictions in one metro. Going multi-metro multiplies
   permits, companies and relationships together, and the company-resolution step is the
   part that scales worst.
4. **No read isolation.** Any customer-facing read contends directly with ingestion.

Postgres becomes necessary when two of these bind at once — realistically at multi-metro
expansion or at external API launch, not before.

## 5. Are we preserving enough historical data?

**No. This is the central finding of the audit.**

| Question the business wants to answer | Can we today? |
| --- | --- |
| When did this project first appear? | Approximately, at permit granularity only |
| When did its valuation change? | **No** — prior values overwritten |
| When was a contractor first associated with it? | Partially, from permit dates not observation dates |
| How long was the project active? | **No** — no project entity, no state timeline |
| When did the permit status change? | **No** — only current status exists |
| Which companies increased activity over time? | Only via metrics that are themselves overwritten each run |

The evidence:

- **52,755 permits (47.8%)** have been overwritten at least once, with nothing retained.
- **All 110,298** project rows share `analyzed_at = 2026-09-15` and one `analysis_version`.
  The entire scoring history of the business is one day old by construction.
- **No table anywhere has a `source_updated_at` column.**
- `raw_source_json` is captured but overwritten on every update, so the raw payload cannot
  be replayed or re-mapped.

None of this is recoverable retroactively. Value accrues only from the day capture is
switched on, which is why it is the first recommendation.

## 6. Highest-priority architecture changes

1. **RAW append-only capture with payload hashing.** Never lose a source observation
   again, and get a truthful change signal as a side effect.
2. **A lineage spine** (`source_record_map`) so every canonical entity traces to the source
   records that evidence it, with match method and confidence.
3. **A real project entity** grouping permits by parcel and normalized address within a
   time window — collapsing ~110k permits toward ~50k actual jobs.
4. **Narrow change-event tables** for the attributes that matter commercially: status,
   valuation, lifecycle, contractor attribution.
5. **Per-project relationships with confidence**, replacing three competing
   representations and distinguishing verified source fields from heuristic guesses.
6. **Populated corroborating signals**, principally the AZ ROC licence registry, so entity
   resolution has more than a name to work with.

## 7. What should be done first

**Two things, in this order, before any modelling work.**

**First — move the database out of OneDrive**, enable WAL, guarantee `foreign_keys` on
every connection, add scheduled `VACUUM INTO` backups, and serialise the two schedulers.
This is a few hours of work that removes the largest catastrophic risk in the system, and it is
independent of every architectural decision below it.

**Second — turn on RAW capture.** Create `raw_ingest_batch` and `raw_record`, dual-write
from the existing `upsert_permit()` call site, and write a new version only when the
payload hash changes. It is a single additive write at a single call site, it changes no
application behaviour, and it immediately stops the permanent loss of history.

Everything else — CURATED, project grouping, GOLD, the API — can wait. These two cannot,
because one prevents a catastrophe and the other prevents a permanent, compounding loss.

## 8. What should NOT be changed yet

- **Scoring.** Weights, inputs and the 60-point threshold stay exactly as they are. The
  5,704 projects currently scoring ≥ 60 should be assertable as unchanged at every phase
  gate.
- **Any existing production table.** No renames, no drops, no restructuring. `contractors`
  and `municipalities` get retired only after their consumers are repointed, and
  "retired" means stop writing, not drop.
- **The connectors.** They are the strongest part of the codebase.
- **The database engine.** Stay on SQLite; switching mid-migration would guarantee the
  migration never finishes.
- **CRM behaviour, tenancy and RBAC.**
- **The dashboards**, until compatibility views are in place and validated.

## 9. Estimated implementation sequence

| Stage | Phases | Outcome |
| --- | --- | --- |
| Immediate | 0 — operational safety<br>1 — RAW capture | Corruption risk removed; history starts accruing |
| Near | 2 — lineage spine<br>3 — CURATED shadow build<br>4 — change events | Provenance and history become queryable; app untouched |
| Mid | 5 — project entity<br>6 — enrichment + resolution upgrade | The product's headline noun exists; resolution gets real signals |
| Mid-late | 7 — application cutover<br>8 — GOLD marts | App runs on the new foundation |
| Late | 9 — data product foundations<br>Tier 2 licensed exports | First external revenue |
| Later | Read API, probably on Postgres | Full data product |

Phases 0 through 4 are invisible to the application. Phase 7 is the only one that touches
production read paths, and it proceeds one module at a time behind compatibility views,
with a one-line rollback each.

## 10. Major technical risks

| Risk | Assessment |
| --- | --- |
| **OneDrive corrupts the database** | Medium likelihood, critical impact. Mitigated entirely by Phase 0. Do it first. |
| **Project grouping over-merges distinct jobs** | High likelihood, high impact. The top addresses carry 634, 410 and 293 permits — those are either genuine mega-developments or address-quality artefacts, and only hand validation will tell you which. Mitigated by confidence scoring and full rebuildability. |
| **Migration stalls half-finished** | The realistic risk. Mitigated structurally: every phase is independently valuable, and Phases 0 and 1 alone justify the effort. |
| **Cutover breaks a dashboard** | Medium/high. Mitigated by compatibility views, per-module cutover, and diffing exports against a pre-cutover baseline. |
| **Re-resolution merges genuinely distinct companies** | Medium/high. Never auto-merge on new evidence; keep merges manual and audited. |
| **Redistributing data we lack rights to** | Low now, critical at launch. No source's terms have been assessed. `redistribution_allowed` must default to "no". |
| **Scoring drift during migration** | Low likelihood, critical impact. Scoring is explicitly out of scope; assert the ≥60 count at every gate. |
| **RAW growth outpaces SQLite** | Low. Hash deduplication holds growth to roughly 100–150 MB/year. |

---

## Documents in this set

| File | Contents |
| --- | --- |
| `01_CURRENT_STATE_AUDIT.md` | Database inventory, keys, indexes, entity model, history behaviour, application dependencies |
| `02_SOURCE_LINEAGE.md` | Source registry, end-to-end lineage map, orchestration, where traceability is lost |
| `03_DATA_QUALITY_RISKS.md` | 14 ranked, quantified risks with the SQL evidence behind each |
| `04_TARGET_ARCHITECTURE.md` | RAW → CURATED → GOLD design, history strategy, entity resolution, technology trajectory |
| `05_PROPOSED_SCHEMA.md` | Full proposed DDL (not applied) plus safe indexes for existing tables |
| `06_MIGRATION_PLAN.md` | Ten phases with exit criteria, rollback and a risk register |
| `07_DATA_PRODUCT_ROADMAP.md` | Product tiers, endpoint requirements, PII and licensing governance |
