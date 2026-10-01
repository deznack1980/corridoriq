# 06 — Migration Plan

**Status:** proposal. No phase below has been started. Phase 2 work begins only on
explicit instruction.

**Governing rule for every phase:** the application keeps working, unchanged, throughout.
If a step requires the app to change behaviour in order to proceed, the step is wrong.

---

## Sequencing at a glance

| Phase | Name | Risk | Reversible | Depends on |
| --- | --- | --- | --- | --- |
| 0 | Operational safety | Low | Yes | — |
| 1 | RAW capture (dual-write) | Low | Yes | 0 |
| 2 | Lineage spine | Low | Yes | 1 |
| 3 | CURATED shadow build | Low | Yes | 2 |
| 4 | Change events | Low | Yes | 3 |
| 5 | Project entity | Medium | Yes | 3 |
| 6 | Enrichment + resolution upgrade | Medium | Yes | 2, 5 |
| 7 | Application cutover | **High** | Yes, per module | 3, 5 |
| 8 | GOLD marts | Low | Yes | 7 |
| 9 | Data product foundations | Medium | Yes | 8 |

Phases 0–4 change nothing the application can observe. Phase 7 is the only phase that
touches production read paths, and it is done one module at a time behind compatibility
views.

---

## Phase 0 — Operational safety

**Why first:** none of this matters if the database file is corrupted by a sync conflict.
These are the cheapest, highest-value actions in the entire plan and they are independent
of every architectural decision.

1. **Move the database out of the OneDrive-synced tree** — e.g. `C:\CorridorIQData\`, with
   `DB_PATH` read from an environment variable and defaulted for local dev. A 283 MB
   SQLite file under continuous cloud sync, in rollback journal mode, with a scheduled
   writer and a long-lived server process, is a corruption incident waiting to happen.
2. **Enable WAL** (`PRAGMA journal_mode=WAL`) so readers stop blocking on the writer.
3. **Guarantee `foreign_keys` on every connection.** There is no file-level
   setting in SQLite to enable — the pragma resets to OFF on each new
   connection — so the guarantee has to come from all code routing through
   `get_connection()`, verified by a check that fails if it ever does not.
4. **Stamp `user_version`** and start tracking schema revisions explicitly.
5. **Scheduled backups** — `VACUUM INTO` a timestamped snapshot nightly, retain 14 daily
   and 8 weekly, and verify one restore.
6. **Serialise the two schedulers** — `automation/run_daily.ps1` and the morning refresh
   can currently overlap. Extend the existing `pipeline_runs` lock to cover both.
7. Add the five safe indexes listed in `05` §7.

**Exit criteria:** a verified restore from backup; WAL active; no scheduler overlap
possible; all existing tests green.

---

## Phase 1 — RAW capture (dual-write)

**The single most urgent phase.** Historical value accrues only from the day this is
switched on.

Scope:

- Create `raw_ingest_batch` and `raw_record`.
- Open a batch at the top of `ingest_jurisdiction()`; close it at the end with counts.
- In the connector loop, before `upsert_permit()`, hash the raw payload and write a
  `raw_record` **only when the hash differs** from the current version.
- `upsert_permit()` continues to behave exactly as it does today. RAW is written
  alongside, never instead.
- Backfill one initial RAW version per permit from the existing `permits.raw_source_json`,
  tagged with a synthetic backfill batch and `fetched_at = first_seen_at`. This is a
  one-time snapshot, honestly labelled — it is not real history, it is the starting point.

Why this is low-risk: it is purely additive, it is a single write in a single call site,
and if it fails the existing pipeline is unaffected.

Immediate side benefit: hash comparison exposes the churn measured in `03` §7 (Goodyear's
245 records producing 12,250 phantom updates) and gives a truthful daily change count for
the first time.

**Exit criteria:** every ingest run produces a batch row; `records_changed` is materially
lower than today's `records_updated` for Goodyear and Chandler; RAW row count grows only
on genuine change; application behaviour byte-identical.

---

## Phase 2 — Lineage spine

Scope:

- Create `source_record_map`.
- Backfill permit identity: one `('is')` row per permit from `(jurisdiction,
  permit_number)` with `match_method='permit_natural_key'`, `resolution_tier='EXACT'`.
- Backfill company lineage by replaying the existing resolution reasons: for each
  permit→company link, write a `contractor_of` / `owner_of` row with the method and
  confidence the matcher would produce. `company_identity_audit_log` (12,637 rows) supplies
  history where available.
- Label the heuristic attributions explicitly — Phoenix's `PERMIT_NAME` fallback and
  Tempe's `ProjectName` fallback get `match_method='heuristic_project_name'` and a lower
  confidence than authoritative contractor fields.
- Extend `company_resolution` to write `source_record_map` rows going forward.

**Exit criteria:** every one of the 12,636 companies traces to at least one source record;
the count of heuristically-attributed relationships is known and reportable; no change to
application behaviour.

---

## Phase 3 — CURATED shadow build

Build `curated_*` tables from existing production tables plus RAW. Read-only for the
application; nothing reads these yet.

Order: `curated_jurisdiction` (merging `jurisdictions` + `municipalities`) →
`curated_parcel` → `curated_location` → `curated_permit` → `curated_company` +
`curated_company_alias` → `curated_project_company`.

Two data-quality fixes land here, in CURATED only, leaving production untouched:

- **Date normalisation** — coerce Mesa's 10,485 ISO-timestamp values to strict
  `YYYY-MM-DD` so all 110,298 rows sort and compare identically.
- **Valuation semantics** — set `valuation_is_source_null` so Phoenix's structural absence
  (49,510 rows) is distinguishable from a reported zero.

Then build a reconciliation report: row counts, sums and spot-check joins, CURATED vs
production. Any discrepancy is a build bug, and CURATED is simply rebuilt — it is
derivable by definition.

**Exit criteria:** CURATED reconciles to production within a documented tolerance; a full
rebuild from scratch completes inside the morning-refresh window.

---

## Phase 4 — Change events

Scope:

- Create the three `*_change_event` tables.
- In the CURATED upsert path, diff the tracked attributes and emit an event per change,
  carrying `raw_record_id` and `batch_id`.
- Backfill what is honestly recoverable — which is very little. Prior values were
  overwritten and are gone. Do **not** manufacture synthetic history; start the series at
  go-live and say so in the data dictionary.

**Exit criteria:** "when did permit X change status" is answerable by a single indexed
query for all changes after go-live.

---

## Phase 5 — Project entity

The highest-value modelling change, and the first with real judgement in it.

Scope:

1. Build `curated_location` address normalisation and measure its collapse rate against
   the known baseline (48,103 distinct addresses over 110,298 permits).
2. Group permits into `curated_project`: parcel where available (32,424 distinct values),
   else normalized address plus jurisdiction, plus a time-proximity window so a 2019 and a
   2026 job at one address stay separate.
3. Record `grouping_method` and `grouping_confidence` on every project and link.
4. Populate `curated_project_permit`.
5. **Hand-validate the extremes** before accepting the result — `32200 N 43RD AVE` (634
   permits), `5088 W INNOVATION CIR` (410), `2800 W SONORAN DESERT DR` (293). These are
   either genuine mega-developments or address-quality artefacts, and the difference
   determines whether the grouping rule is right. An automated metric will not tell you
   which; a human looking at ten addresses will.
6. Tune the time window empirically and record the chosen value with its rationale.

Expected outcome: ~110k permits collapse toward ~48k–60k real projects.

**This phase does not touch `projects`, `opportunity_score`, or the 60-point threshold.**
Job-level scoring is a separate, later, explicitly-approved decision.

**Exit criteria:** grouping validated by hand on the top 20 multi-permit addresses; a
documented false-grouping rate; the existing `projects` table untouched.

---

## Phase 6 — Enrichment and resolution upgrade

Scope, in value order:

1. **AZ ROC licence registry** — the highest-leverage single addition. Lifts licence
   coverage from 1.7% and supplies legal name, DBA, status, address and classification,
   giving the matcher the corroborating signals it was designed for but has never had.
2. **Parcel / assessor data** — resolves the 10,826 distinct `owner_name` values into real
   owner entities and separates individuals from LLCs, which also serves the PII work.
3. **Geocoding** — only 28.8% of permits currently have coordinates; every geographic
   product is capped until this improves.
4. **Resolution tiers persisted** — `EXACT` / `HIGH_CONFIDENCE` / `PROBABLE` /
   `UNRESOLVED` written to `source_record_map` and `curated_company`.
5. **Re-resolve companies** with the new evidence, routing anything uncertain to the
   review queue. Never auto-merge on new evidence alone.
6. **Retire `contractors`** once `curated_company` provably covers it — including the 748
   contractor rows that currently have no matching company. Retire means stop writing and
   stop reading; the table stays.

Each new source enters through RAW like any other. No enrichment writes directly to
CURATED.

**Exit criteria:** licence coverage materially improved; duplicate-company rate measured
against a hand-labelled sample rather than assumed from the current tautological zero;
review queue worked down.

---

## Phase 7 — Application cutover (**highest risk**)

Scope:

1. Create the compatibility views from `05` §6.
2. Repoint **one module at a time**, in ascending order of blast radius:
   `export_json.py` → reports → `company_resolution/queries.py` → `crm/admin.py` →
   `crm/service.py` (40 statements, last).
3. After each module: run the full test suite, diff the generated exports against the
   pre-cutover baseline, and confirm the dashboards render.
4. Keep the old code path behind a settings flag until the whole surface is migrated.

Rollback is a one-line table-name change per module, which is exactly why the
compatibility views exist.

**Exit criteria:** every dashboard and export byte-identical (modulo the deliberate date
normalisation) before and after; the 60-point threshold still selecting the same 5,704
projects; full test suite green.

---

## Phase 8 — GOLD marts

Build `gold_company_profile` and `gold_project_activity` first. Move
`company_intelligence` semantics into GOLD as a derived, rebuildable mart rather than an
in-place-mutated table. Add `generated_at` and `source_watermark` everywhere. Rebuild
incrementally, driven by the change events from Phase 4.

Do not build eight marts for customers who do not exist yet.

---

## Phase 9 — Data product foundations

Not the public API — the things that must exist before one can be built:

- opaque `public_id` on every externally-visible entity
- cursor pagination and a trustworthy change-feed watermark
- per-source licence and redistribution flags enforced as a hard filter
- PII suppression for `owner_name` and individual `property_owner` entities
- published coverage and freshness metadata per jurisdiction
- a data dictionary and a stated SLA
- entitlements and rate limiting, distinct from the internal session RBAC

---

## Risk register

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| OneDrive sync corrupts the DB | Medium | **Critical** | Phase 0, item 1 — do this first, independent of everything else |
| Project grouping over-merges distinct jobs | **High** | High | Confidence scoring, hand validation of the top 20 addresses, tunable time window, fully rebuildable |
| RAW growth outpaces SQLite | Low (3–5 yr) | Medium | Hash-based dedup keeps growth to ~100–150 MB/yr; defined Postgres trigger points in `04` §8 |
| Cutover breaks a dashboard | Medium | High | Compatibility views, one module at a time, export diffing, settings-flag rollback |
| Scoring drifts during migration | Low | **Critical** | Scoring is explicitly out of scope; assert the 5,704-project count at every phase gate |
| Re-resolution merges distinct companies | Medium | High | Never auto-merge on new evidence; review queue; merges stay manual and audited |
| Two schedulers collide mid-migration | Medium | Medium | Phase 0, item 6 |
| Migration stalls half-done | **High** | High | Every phase is independently valuable and independently shippable; Phases 0 and 1 alone justify the work |
| Redistribution of non-redistributable source data | Low now, High at launch | **Critical** | `redistribution_allowed` defaults to unassessed and is a hard filter in Phase 9 |

The stalling risk is the realistic one. It is mitigated structurally: Phase 0 is worth
doing even if nothing else happens, and Phase 1 is worth doing even if CURATED is never
built.

---

## Explicitly out of scope

- Changing scoring weights, inputs, or the 60-point threshold
- Renaming, dropping or restructuring any existing production table
- Migrating off SQLite
- Building the public API
- Changing CRM behaviour or the tenancy model
- Rewriting working connectors — they are the strongest part of the codebase
