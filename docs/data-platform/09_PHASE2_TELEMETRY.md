# Phase 2 — Ingestion and Source Health Telemetry

Executed 2026-09-15. Scope was database safety, RAW append-only capture,
ingestion/source-health telemetry, Chandler dedupe, and testing. Four of those
five landed in Phase 0/1 (see `08_PHASE0_PHASE1_IMPLEMENTATION.md`); this
document covers the remaining one.

Project regrouping and product behaviour were not touched.

---

## Why

Phase 1 proved the pipeline was rewriting unchanged rows. Phase 2 asks a
different question: **is each source actually working?**

The trigger was `gilbert_az`. It failed on 2026-09-08, 09-13 and 09-15, and the
pattern was only discovered by hand-writing SQL against `ingestion_runs`. Every
individual run looked like an isolated blip. Nothing aggregated them, so nothing
noticed.

---

## What changed

### 1. `ingestion_runs` widened from "did it work" to "what happened"

The original table had six counters and a free-text error string. A message like
`500 Server Error: ... for url: https://...` cannot be grouped or counted, so
"this source fails with 5xx about once a week" was an investigation rather than
a query.

Added (all nullable — runs recorded before Phase 2 genuinely lack these values,
and a backfilled zero would be indistinguishable from a measured zero):

| Column | Answers |
| --- | --- |
| `error_type`, `http_status` | why it failed, in a groupable form |
| `duration_ms` | is this source getting slower |
| `requested_since` | which watermark was actually used |
| `retries` | did it only succeed on the third attempt |
| `source_rows`, `duplicates_dropped` | what the source sent vs what was usable |
| `records_unchanged`, `raw_new`, `raw_changed`, `raw_unchanged` | did the fetched records contain anything new |
| `pipeline_run_id`, `raw_batch_id`, `connector_type` | joins to the orchestrating run and the RAW batch |

`classify_error()` reduces an exception to a short token: `http_5xx`,
`http_4xx`, `rate_limited`, `timeout`, `connection`, `network`, `auth`,
`invalid_request`, `not_configured`, `unknown`.

Writing telemetry is best-effort. If the columns are missing, the insert falls
back to the original eight and the run is still recorded — losing telemetry must
never mean losing the audit row.

### 2. `source_health_snapshot` — health as a trend, not a reading

A rollup per jurisdiction, evaluated and appended after every ingestion pass, so
reliability accumulates the same way RAW accumulates source history. "Gilbert
has been degraded three weeks running" becomes a query.

States, in precedence order:

| State | Meaning |
| --- | --- |
| `failing` | a sustained failure streak (>= 3 consecutive) |
| `degraded` | an isolated failure, or >= 2 failures in the trailing week |
| `silent` | runs succeed but return nothing, for a source that used to return something |
| `healthy` | recent runs succeeding |
| `unknown` | no runs recorded yet |

**`silent` is the state worth having.** A withdrawn feed, a renamed field, or a
broken incremental filter all produce HTTP 200 with zero rows, which is
indistinguishable from "no new permits were issued" until enough quiet time has
passed.

**Deliberately not a health input: how old the newest permit is.** A city that
is not issuing permits is not a broken source. Chandler's feed legitimately
tops out at 2025-10-17 and Goodyear's at 2024-11-20; driving state from that
would paint them permanently red and train everyone to ignore the report. The
figure is reported alongside the state instead, and a test pins this behaviour.

### 3. Automatic capture

Snapshots are recorded after **every** jurisdiction has been attempted, in both
orchestration paths — `run_ingestion()` and `run_morning_refresh()` — so a
snapshot always reflects a complete round. Both call sites are guarded: a
reporting failure can never fail an otherwise successful run.

Non-healthy sources are printed inline at the end of an ingest.

---

## What it found immediately

The first live run flagged two sources nobody knew were broken:

```
[health] buckeye_az:    SILENT   - succeeding but no records returned for 27 days
[health] scottsdale_az: SILENT   - succeeding but no records returned for 64 days
[health] gilbert_az:    DEGRADED - 5 failures in the last 7 days
```

`scripts/check_source_gaps.py` then compared what each source publishes against
what we hold:

| Source | Held | Our newest | Source newest | Never collected |
| --- | ---: | --- | --- | ---: |
| buckeye_az | 13,267 | 2026-08-16 | 2026-09-12 | **432** |
| scottsdale_az | 15,484 | 2026-07-10 | 2026-09-11 | **129** |
| tempe_az | 5,786 | 2026-09-03 | 2026-09-11 | **71** |
| gilbert_az | 12,101 | 2026-09-10 | 2026-09-11 | **34** |
| mesa_az | 10,485 | 2026-09-10 | 2026-09-11 | **3** |
| peoria_az | 3,335 | 2026-09-14 | 2026-09-14 | 0 |
| phoenix_az | 49,510 | 2026-09-14 | 2026-09-14 | 0 |
| chandler_az | 85 | — | — | full refresh |
| goodyear_az | 245 | — | — | full refresh |

**At least 669 permits have been published by sources and never collected.**

### Root cause: the watermark is on the wrong clock

`_parse_since()` uses `jurisdictions.last_synced_at` — *when we last ran* — and
the connectors then filter on the source's **issue date**:

```sql
WHERE IssueDate >= TIMESTAMP '<when we last ran>'
```

Sources do not publish instantly. A permit issued on 2026-09-05 but added to the
feed on 2026-09-14 is invisible to a run whose watermark is 2026-09-10, and
because every run advances `last_synced_at` to now, **it can never be picked up
again**. The gap is permanent and silent.

This explains the pattern exactly: Phoenix and Peoria publish promptly and show
no gap, while Scottsdale — which lags by weeks — has collected nothing since
2026-07-10 while its feed gained records through 2026-09-11.

### The fix

`compute_since()` replaces `_parse_since()` at both call sites. The watermark is
now **the newest source date already held, minus a lookback**, rather than
wall-clock run time. `_parse_since()` survives only as the first-run fallback
for a source with no records yet.

The lookback is 30 days (`INGEST_WATERMARK_LOOKBACK_DAYS`). Sizing it was not
obvious: measured publish lag topped out at 5 days, but **that measurement is
survivorship-biased** — it can only see records we managed to catch, and
anything slower was dropped before it could enter the sample. 30 days is a
deliberate margin over a figure known to be biased low. Re-fetched records that
have not changed are detected as unchanged and do not churn, so a wide window
costs query volume and nothing else. A test pins that property.

### The catch-up

`scripts/catchup_ingest.py --lookback-days 400`, run once on 2026-09-15.

**5,432 permits were recovered — eight times the 669 the gap checker predicted.**

| Source | Recovered | Predicted by gap check |
| --- | ---: | ---: |
| phoenix_az | 1,469 | **0** |
| buckeye_az | 1,023 | 432 |
| gilbert_az | 989 | 34 |
| mesa_az | 920 | 3 |
| tempe_az | 484 | 71 |
| peoria_az | 397 | 0 |
| scottsdale_az | 150 | 129 |

The discrepancy is the most useful finding of Phase 2. `check_source_gaps.py`
only counts records dated *after* the newest one held, so it can only see
backlog at the leading edge. It cannot see **interior holes** — a permit issued
in March and published in April, when the watermark had already passed April.
Those holes are invisible to every check we had.

Phoenix proves the point: the gap check reported it perfectly current, and it
was — at the leading edge. It was still missing 1,469 records from inside its
own history. The sources that looked healthiest were quietly the worst affected,
because a prompt-publishing source gets a fast-moving watermark, and a
fast-moving watermark drops more late arrivals.

### After

All nine sources report zero gap. Both `silent` states cleared. Permit count
110,298 -> 115,730. `gilbert_az` remains `degraded`, which is accurate: it is
genuinely flaky, and is now recovering.

---

## Tests

35 new tests (`test_source_health.py`, `test_watermark.py`), 237 passing
overall. Beyond the happy paths, they pin the judgement calls:

- an intermittent source that always recovers is still reported as `degraded`
- a briefly empty source is **not** `silent`; a long-empty one is
- a newly connected source with no data yet is **not** `silent`
- a quiet city with old permit dates is **not** unhealthy
- telemetry failure cannot break ingestion
- a database without the Phase 2 columns still records its runs
- the watermark ignores `last_synced_at`, including a future-dated one
- a record issued before the last run but published after it **is** collected
- the old rule provably would have missed that record
- re-fetching unchanged records across a wide lookback does not churn them

---

## Commands

| Task | Command |
| --- | --- |
| Source health report | `python -m pipeline.telemetry.source_health` |
| Record a snapshot | `python -m pipeline.telemetry.source_health --persist` |
| Health trend for one source | `python -m pipeline.telemetry.source_health --history <slug>` |
| As JSON | `python -m pipeline.telemetry.source_health --json` |
| Coverage gaps vs live sources | `python scripts/check_source_gaps.py` |
| One-off deep catch-up | `python scripts/catchup_ingest.py --dry-run` |
| Diagnose a failing source | `python scripts/diagnose_source.py <slug>` |
| Pipeline step | `python -m pipeline.run source_health` |

The health CLI exits `1` when any source is `failing` or `silent`, so it can be
used as a scheduled check.

---

## Open

- **Downstream tables have not been rebuilt.** The catch-up added 5,432
  permits; `projects` still holds 110,298. Grouping and scoring were left alone
  because regrouping was explicitly out of scope. The scheduled 06:00 pipeline
  runs the full chain and will reconcile this on its next pass.
- **Publish lag should be re-measured.** The 5-day figure was taken from a
  biased sample. Now that late arrivals are actually collected, the same query
  will show the true distribution and can justify tightening or widening the
  30-day window.

## Not done, deliberately

- **No API or dashboard surface.** Health is CLI and table only. Adding an
  endpoint would be product behaviour, which was out of scope.
- **No alerting.** The data supports it; nothing sends anything.
- **Scheduler serialisation.** The 05:00 refresh and 06:00 pipeline can still
  overlap.
