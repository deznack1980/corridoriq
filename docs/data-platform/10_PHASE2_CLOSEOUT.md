# Phase 2 Closeout — Evidence and Risk Register

2026-09-15. Evidence for the claims made in `08_` and `09_`, plus the risks
that remain open. All checks below are read-only and reproducible.

---

## 1. RAW append-only is running

### Code-level: history cannot be rewritten

There is **no `DELETE FROM raw_record` or `DELETE FROM raw_ingest_batch`
anywhere in the codebase**, production or test. Exactly two `UPDATE` statements
touch `raw_record`, both in `pipeline/ingestion/raw_capture.py`:

| Line | Statement | What it mutates |
| --- | --- | --- |
| 230 | hash-rule correction | `payload_hash`, `payload_hash_version` |
| 247 | supersede previous version | `is_current` |

Neither touches `payload_json`, `version_number`, `fetched_at`, or `batch_id`.
The observed source payload and its ordering are immutable once written; the
only mutable fields are a derived digest and a pointer to which row is current.

### Data-level: invariants hold across 126,000 versions

```
raw_record rows (versions) : 126,000
distinct source records    : 115,730
current versions           : 115,730
superseded versions kept   :  10,270

records without exactly one is_current : 0   PASS
records with gapped/missing v1 history : 0   PASS
current version is not the newest      : 0   PASS
later version fetched earlier in time  : 0   PASS
versions with no owning batch          : 0   PASS
versions with empty payload            : 0   PASS
```

`current versions` equalling `distinct source records` exactly, with 10,270
superseded rows retained alongside, is the append-only property stated
numerically: every record has exactly one current version and history is kept
rather than overwritten.

Version-1 rows from the original 2026-07-08 baseline are still present with
their original `fetched_at`, so nothing has been trimmed or back-dated:

```
mesa_az  PMT23-12497  2026-07-08T03:33:54+00:00
mesa_az  PMT24-02123  2026-07-08T03:33:54+00:00
```

Depth: 105,735 records at one version, 9,819 at two, 175 at three or more.
52 batches, all `succeeded`.

---

## 2. Controlled example

`phoenix_az / 26004742`, the deepest history in the corpus at 8 versions.

```
v1  2026-07-15T02:05:37Z  batch=1   hash=a5de656578ff  [superseded]
v2  2026-09-15T09:27:15Z  batch=45  hash=5ace3b87a48d  [superseded]
      OBJECTID:         1226527 -> 1226523
      STREET_FULL_NAME: '...PKWY N 141' -> '...PKWY N 242'
v3  2026-09-15T09:27:15Z  batch=45  hash=6c1f188f70e0  [superseded]
      STREET_FULL_NAME: '...PKWY N 242' -> '...PKWY N 143'
...
v8  2026-09-15T09:27:15Z  batch=45  hash=7065b4663cf2  [CURRENT]
      STREET_FULL_NAME: '...PKWY N 241' -> '...PKWY N 244'
```

The `permits` table holds **one** row for this permit, `last_updated_at`
2026-09-15. Seven prior observations exist only in RAW. That is the layer doing
its job: the operational table is a current-state projection, and without RAW
those observations would be unrecoverable.

### What this example also exposed

Versions 2 through 8 were all written in **the same batch, at the same second**.
That is not change over time — it is seven *distinct source rows* sharing one
permit number, each superseding the last. Phoenix issues one permit number
across multiple units and addresses (`N 141`, `N 242`, `N 143`, ...), and
`source_record_id` is the permit number.

Separating genuine change (versions across different batches) from key
collisions (versions within one batch):

| Source | Multi-version | Real change | Collided | Source rows collapsed |
| --- | ---: | ---: | ---: | ---: |
| buckeye_az | 2,206 | 2,206 | 0 | 0 |
| peoria_az | 2,616 | 2,616 | 0 | 0 |
| phoenix_az | 1,729 | 1,639 | 90 | 102 |
| gilbert_az | 1,133 | 1,133 | 0 | 0 |
| mesa_az | 1,145 | 1,140 | 5 | 7 |
| chandler_az | 85 | 0 | 85 | 85 |
| others | 1,081 | 1,081 | 0 | 0 |
| **TOTAL** | **9,995** | **9,815** | **180** | **194** |

**98% of version history is genuine change.** The 194 collapsed rows are 0.15%
of the corpus, so this is a defect to track rather than an emergency — but it is
real, it is invisible in `permits`, and only RAW makes it measurable. Chandler's
85 are historical, from batches predating the dedupe fix.

Mesa's collisions differ in `job_value` and coordinates rather than address
(`7315220.24` vs `7302499` for the same permit), which is a different problem
again — the source contradicting itself rather than packing multiple units into
one key.

---

## 3. Publish lag re-measured

**The honest result: true publish lag is still unmeasured, and cannot be
measured yet.** The reasoning matters more than a number here.

The catch-up recovered 5,503 permits in one day. Measuring lag as
`first_seen_at - issued_date` over that cohort returns a large figure, but it
measures **how long the watermark defect ran**, not how late sources publish.
Using it to size the window would repeat the original mistake in the opposite
direction.

What can be stated:

**Upper bound**, for recovered records issued within 45 days of catch-up. Lag
cannot exceed days-since-issue, so these are ceilings, not measurements:

| Source | n | p50 | p90 | max |
| --- | ---: | ---: | ---: | ---: |
| phoenix_az | 823 | 28 | 42 | 45 |
| buckeye_az | 647 | 24 | 40 | 45 |
| gilbert_az | 608 | 26 | 41 | 43 |
| mesa_az | 516 | 27 | 41 | 43 |
| tempe_az | 303 | 25 | 42 | 43 |
| peoria_az | 247 | 19 | 41 | 43 |
| scottsdale_az | 88 | 26 | 39 | 42 |

The near-uniform spread is the signature of a backlog accumulating steadily,
not of a lag distribution.

**The old biased sample**, for contrast — records first seen 2026-08-01 to
09-14, under the broken watermark: n=1,049, p50=2, p90=4, **max=5**. That
maximum is a property of the window, not of the sources. Anything slower was
never collected, so it could not appear in the sample used to judge the window.

### When this becomes measurable

The first unbiased sample is records first seen **from 2026-09-16 onward**,
under the fixed watermark and after the backlog is cleared:

```sql
SELECT jurisdiction,
       CAST(julianday(substr(first_seen_at,1,10))
          - julianday(substr(COALESCE(issued_date, filed_date),1,10)) AS REAL) AS lag_days
FROM permits
WHERE first_seen_at >= '2026-09-16'
  AND COALESCE(issued_date, filed_date) IS NOT NULL;
```

Worth running around 2026-10-16, once ~30 days have accumulated. Two caveats
for whoever runs it: the sample is still censored at the 30-day lookback, so a
p95 approaching 30 means the window is too small rather than comfortably sized;
and `check_source_gaps.py` returning non-zero at the leading edge is the earlier
warning that the window is being outrun.

---

## 4. Restore drill

A backup that has never been opened is a hope, not a backup. The newest backup
was copied to scratch and queried as an independent database:

```
source backup : corridoriq_20260915T093002Z.db (415 MB)
integrity_check      : ok            PASS
foreign_key_check    : 0 violations  PASS

permits 115,730   projects 110,298   companies 12,636
raw_record 126,000   raw_ingest_batch 52
ingestion_runs 587   source_health_snapshot 18   users 4

catch-up permits present in backup : 5,503   PASS
superseded RAW history in backup   : 10,270  PASS
sample query works                 : phoenix newest 26010339 @ 2026-09-14
```

The backup is current, internally consistent, and answers real queries.

---

## 5. Risk register

| # | Risk | Severity | Notes |
| --- | --- | --- | --- |
| 1 | ~~**Backups share a disk with production.**~~ **RESOLVED 2026-09-15** — verified backups now replicate to Cloudflare R2 after every backup run, 30 daily / 26 weekly off-site. | ~~High~~ Closed | `docs/operations/offsite_backup_r2.md`. Replication is guarded: a network failure never fails the local backup. Residual — the R2 token can delete replicas, so ransomware could propagate off-site; object versioning would close that. |
| 2 | **Chandler and Goodyear coverage looks implausibly thin.** Chandler holds 85 permits (newest 2025-10-17), Goodyear 245 (newest 2024-11-20). Both report `healthy` because they return rows. | **High** | 85 permits for a city of ~280,000 suggests a filtered or partial layer, not a complete feed. Health cannot detect this — the source is consistent, just possibly wrong. Needs a look at the endpoint. |
| 3 | **Publish lag unmeasured; 30-day window is reasoned, not measured.** | Medium | If any source's real lag exceeds 30 days, records are dropped again and silently. Mitigated by `check_source_gaps.py` currently reporting 0 across all sources. Re-measure ~2026-10-16. |
| 4 | **Source key collisions.** 180 records, 194 source rows collapsed, because distinct source rows share a permit number. | Medium | 0.15% of corpus and RAW preserves them, but the product never sees them. Fixing means a composite source key, which changes identity — out of scope here. |
| 5 | **Scheduler overlap.** 05:00 morning refresh and 06:00 full pipeline both write and are not serialized. | Medium | WAL plus `busy_timeout=10000` reduces the risk but does not remove it. Phoenix's catch-up ingest ran 4.5 minutes, so overlap is plausible under load. |
| 6 | **Gilbert is genuinely flaky.** 5 failures in the trailing 7 days; currently recovering. | Medium | Correctly reported as `degraded`. Upstream ArcGIS instability, not our defect. |
| 7 | **Nothing alerts.** Health is computed and persisted; no one is notified. | Medium | The CLI exits non-zero on `failing`/`silent`, so it can be wired to a scheduled check cheaply. |
| 8 | **Failure telemetry unexercised in production.** `error_type` has 0 populated rows because nothing has failed since Phase 2 landed. | Low | Classification is covered by tests across 5xx/4xx/429/timeout/connection, but has not yet run against a live failure. |
| 9 | **`projects` lags `permits`** — 110,298 vs 115,730. | Low | Expected and transient. The 06:00 chain reconciles it. |
| 10 | **RAW growth has no retention policy.** 126,000 versions / 87 MB payload today; the catch-up alone added 10,270 superseded versions. | Low | Currently 1.089 versions per record. Fine at this scale; needs a policy before it isn't. |
| 11 | **Single-node SQLite**, 418 MB, one writer. | Known | Architectural, documented in the Phase 1 audit. |

### Recommended order

Risks 1 and 2 are the two worth acting on now. The first is a single command's
worth of change (copy backups off the volume) against total data loss. The
second is the possibility that two jurisdictions have been substantially
missing from the product all along — and unlike the watermark defect, no
telemetry we have will surface it, because the source is behaving consistently.

---

## 6. Reproducing this

| Check | Command |
| --- | --- |
| Operational health | `python -m pipeline.db.doctor` |
| Source health | `python -m pipeline.telemetry.source_health` |
| Coverage gaps vs live sources | `python scripts/check_source_gaps.py` |
| Full test suite | `python -m pytest pipeline/tests -q` |

237 tests passing, 1 skipped.
