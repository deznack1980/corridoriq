# Phase 0 + Phase 1 — Implementation Record

Executed 2026-09-15. Two objectives: remove the catastrophic operational risk,
and stop losing source history. Project regrouping was explicitly **not**
touched — that decision waits for evidence, which this work now starts
collecting.

---

## Phase 0 — Operational safety

### What changed

| Before | After |
| --- | --- |
| `pipeline/db/corridoriq.db` inside OneDrive **and** inside the repo | `C:\CorridorIQData\db\corridoriq.db`, local and unsynced |
| `journal_mode = delete` | `journal_mode = WAL` |
| `synchronous = FULL` (default) | `synchronous = NORMAL` (safe under WAL) |
| `busy_timeout = 5000` | `busy_timeout = 10000` |
| No backups | Verified `VACUUM INTO` backups with 14-daily / 8-weekly retention |
| No way to check health | `python -m pipeline.db.doctor` |

The database location is now resolved from configuration rather than hardcoded:

1. `CORRIDORIQ_DB_PATH` — full path to the `.db` file
2. `CORRIDORIQ_DATA_DIR` — data root; database at `<root>/db/corridoriq.db`
3. platform default — `C:\CorridorIQData` on Windows, `~/.local/share/corridoriq` elsewhere

### The relocation

`scripts/migrate_database_location.py` refuses to give anything up before the
copy is proven:

1. `integrity_check` the source
2. record a manifest of all 43 tables and their row counts
3. copy with `VACUUM INTO` (refuses to overwrite an existing target)
4. `integrity_check` the copy and compare the manifest row for row
5. only then enable WAL and retire the original

Result: 813,467 rows across 43 tables verified identical, 282.91 MB compacted
to 271.87 MB. The original was **moved, not deleted**, to
`C:\CorridorIQData\backups\pre_migration_20260915T082641Z_corridoriq.db`, which
also removed it from the synced folder.

### Correction to the audit

The audit recommended enabling `foreign_keys` "at the file level". That is not
possible: `PRAGMA foreign_keys` is per-connection and resets to OFF on every new
connection. SQLite has no persistent setting. Enforcement therefore depends on
all code routing through `get_connection()` — production code does, and the
doctor verifies it. The audit documents have been corrected.

### A real bug found during the move

The first relocation attempt failed to retire the original with `WinError 32`
(file in use). The initial diagnosis — an OneDrive sync lock — was wrong. The
cause was in the migration script itself: `with sqlite3.connect(...)` scopes a
*transaction*, it does not close the connection. The script was holding its own
read handle open. Fixed with an explicit closing context manager, and covered by
a test that performs a full copy-verify-retire cycle.

### Guard against silent data loss

If the configured database is missing but a legacy in-repo one still exists,
`get_connection()` now raises instead of silently creating an empty database —
otherwise a mistyped `CORRIDORIQ_DB_PATH` looks exactly like losing 110k permits.

---

## Phase 1 — RAW append-only capture

### The layer

Two tables, written alongside the existing upsert and read by nothing in the
application, so no existing behaviour changes:

- **`raw_ingest_batch`** — one row per ingestion attempt per source: endpoint,
  watermark, timings, status, and new/changed/unchanged counts.
- **`raw_record`** — one row per **observed version** of a source record, with
  the verbatim payload, a content hash, `fetched_at`, `version_number` and
  `is_current`.

Contract: `payload_json` is never altered and nothing is ever deleted. Two
UPDATEs are permitted, both on derived bookkeeping: flipping `is_current` when
a version is superseded, and correcting a stale hash after the hashing rule
changes.

### Two schema decisions that differ from `05_PROPOSED_SCHEMA.md`

**Uniqueness is keyed on version, not hash.** The proposal used
`UNIQUE(source_system, source_record_id, payload_hash)`. That would silently
reject a value that changes A → B → A, which is a real and commercially
interesting event (a permit reverting status). The key is now
`UNIQUE(source_system, source_record_id, version_number)`.

**"Exactly one current version" is enforced by the database**, via a partial
unique index on `(source_system, source_record_id) WHERE is_current = 1`,
rather than by convention in application code.

### Dual-write

Both ingestion paths — `run_ingestion()` (full run) and `ingest_jurisdiction()`
(morning refresh) — capture RAW immediately **before** `upsert_permit`, so what
the source said is preserved even though the permit row is still overwritten in
place.

RAW capture is observability, not a dependency. Any failure in it degrades to a
no-op and ingestion continues: losing a day of permits is worse than losing a
day of raw versions. It can also be disabled entirely with
`CORRIDORIQ_RAW_CAPTURE=0`.

### Baseline snapshot

`python -m pipeline.run raw_backfill` seeded **110,298 permits, 0 skipped**, in
7 seconds, from the existing `permits.raw_source_json`. Re-running captures
nothing (110,298 unchanged), confirming idempotency.

These rows are honestly labelled: the batch's `connector_type` is `backfill`,
and `fetched_at` is set to each permit's `first_seen_at` rather than to the time
the backfill ran. **Version 1 of a backfilled record is a starting point, not
evidence that nothing changed before it.**

---

## Evidence produced on day one

The audit claimed the pipeline rewrites rows that have not changed. RAW
measured it on live data immediately. A single ingest run:

| Jurisdiction | Fetched | Permits "updated" | Actually changed |
| --- | --- | --- | --- |
| goodyear_az | 245 | 245 | **0** |
| chandler_az | 170 | 170 | **0** |

415 permit rows rewritten — every one bumping `last_updated_at` — for zero
actual data change. This is why "when did this permit last change?" has never
been answerable.

### A source-data defect found within minutes

Chandler initially showed 170 changed records against only 85 permits. Probing
the layer directly confirmed the cause is in Chandler's published data:

```
layer        : 'UNDER CONSTRUCTION'  (esriGeometryPoint)
server count : 170 rows
distinct permit numbers : 85
duplication profile     : every one of the 85 appears exactly twice
duplicate pairs identical except OBJECTID : 85 / 85
duplicate pairs with identical geometry   : 85 / 85
```

Every permit is published twice, identical in all attributes **and** geometry,
differing only in `OBJECTID` — an Esri internal row id that is reassigned
between requests.

**Correction to an earlier draft of this document:** I initially wrote that the
`permits` table had been "arbitrarily discarding" one row of each pair. That
overstated the harm. Because the pairs are byte-identical apart from an internal
id, last-write-wins produced the correct result and no data was ever lost. The
real costs were wasted work (every Chandler ingest did twice the upserts it
needed) and inflated `records_fetched` metrics.

Two fixes:

1. **Change detection ignores source-internal fields.** Esri row identity,
   geometry bookkeeping and editor-tracking fields, plus Socrata's
   `:`-prefixed system fields as a family. The full payload is still stored
   verbatim — the exclusion affects hashing only.
2. **Exact duplicates are dropped at the connector.** `BaseConnector.run()`
   now skips a row whose mapped fields are identical to one already seen in the
   same batch, and reports `duplicates_dropped`. Rows sharing a permit number
   but *disagreeing* on any field are both kept — that is a genuine source
   conflict and must stay visible rather than be quietly resolved.

Live result after both fixes:

```
[chandler_az] fetched=85 updated=85 raw_changed=0 dupes_dropped=85
[goodyear_az] fetched=245 updated=245 raw_changed=0
```

### Hash rules are versioned

Changing the hashing rule would otherwise make every record in the corpus look
changed exactly once. `raw_record.payload_hash_version` records which rule
produced a hash; when the rule changes, the hash is recomputed from the stored
payload and corrected in place rather than manufacturing a version bump.

**Known artefact:** ~500 phantom versions exist on Chandler (v4) and Goodyear
(v2) from developing this rule against live data before versioning was added.
They are left in place because the layer is append-only. Out of 110,798 versions
this is immaterial, but it should not be read as real change history.

---

## Current state

```
versions : 110,798
records  : 110,298
observed : 2026-07-08 -> 2026-09-15
```

Tests: **197 passed, 1 skipped**, including 42 new tests covering hashing,
versioning, revert detection, the single-current-version constraint, dual-write
in both ingestion paths, RAW-failure isolation, backfill idempotency, backup
verification, retention, and relocation safety.

---

## Commands

| Task | Command |
| --- | --- |
| Health check | `python -m pipeline.db.doctor` |
| Backup now | `python -m pipeline.db.backup` |
| List backups | `python -m pipeline.db.backup --list` |
| Verify newest backup | `python -m pipeline.db.backup --verify-latest` |
| RAW layer status | `python scripts/raw_status.py` |
| Baseline backfill | `python -m pipeline.run raw_backfill` |
| Apply schema changes | `python scripts/apply_schema.py` |
| Relocate the database | `python scripts/migrate_database_location.py` |
| Diagnose a failing source | `python scripts/diagnose_source.py <slug>` |

---

## Not done, deliberately

- **Project regrouping.** Untouched, as instructed. RAW now accumulates the
  evidence needed to design it.
- **Curated layer.** No `curated_*` tables yet. RAW must accumulate real
  observations first, otherwise the curated design is guesswork.
- **Scheduler serialisation.** The daily pipeline (06:00) and morning refresh
  (05:00) can still overlap. WAL and a 10s busy timeout make this far less
  dangerous, but it is not fixed.
---

## Source health: gilbert_az

Diagnosed with `python scripts/diagnose_source.py gilbert_az`, which probes a
layer at increasing specificity to separate "the service is down" from "our
query is wrong".

**The entire Gilbert ArcGIS Server is returning HTTP 500**, not just our layer:

| Endpoint | Result |
| --- | --- |
| `/arcgis/rest/info` (server root) | HTTP 500 |
| `/arcgis/rest/services` | HTTP 500 |
| `/arcgis/rest/services/OD` | HTTP 500 |
| `.../Growth_Development_Tables_1/MapServer` | HTTP 500 |
| `.../MapServer/3` (our layer) | HTTP 500 |

The body is an ArcGIS Web Adaptor "Application Error" page. Nothing is wrong
with our query, our watermark, or the layer definition — Gilbert's server is
down. No code change can fix it.

Timeline from `ingestion_runs`:

- last successful run: **2026-09-15T07:50:35Z**, so the outage began within the
  hour before it was noticed
- the source is **intermittently flaky**: connection failures also on
  2026-09-08 and 2026-09-13
- exposure is currently low — we hold 12,101 Gilbert permits, newest issued
  date 2026-09-10, last real data pull 2026-09-12 (97 records)

Failure isolation behaved correctly: Gilbert was recorded as `error`, the other
eight jurisdictions completed normally, and `last_synced_at` was not advanced,
so no data will be skipped when the source recovers.

**Watch, do not patch.** If Gilbert is still 500ing in a few days, the next step
is to look for an alternative endpoint (an ArcGIS Online hosted copy or Hub
dataset) rather than to add retries against a server that is entirely offline.

---

## Not done, deliberately (continued)

- **Retry on the full-pipeline ingest path.** `ingest_jurisdiction()` (morning
  refresh) retries transient failures; `run_ingestion()` (full run) does not.
  Given Gilbert's flakiness this is worth aligning, but it changes ingestion
  behaviour and was left out of a stability-focused change.
