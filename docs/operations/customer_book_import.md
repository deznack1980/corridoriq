# Customer-book import and Book × Market report (offline)

Code: `pipeline/book_match/`. Tenancy: see `docs/architecture/tenancy.md`.

## Workflow

```
# 1. register the supplier (once)
python -m pipeline.book_match tenant-create --tenant acme-supply --name "Acme Supply"

# 2. import + match + classify + report
python -m pipeline.book_match <accounts.csv> --tenant acme-supply --out <report dir> [--config policy.json]

# 3. a person completes review.csv, then:
python -m pipeline.book_match review-apply <review_completed.csv> --tenant acme-supply --reviewer "Name"

# 4. re-run step 2 (same file is not re-imported; matching and classification re-run)
```

Options: `--tenant-root` (else `CORRIDORIQ_TENANT_ROOT`, else `<DATA_DIR>/tenants`),
`--intel-db` (else `settings.DB_PATH`; always opened read-only).

## Input file

UTF-8 CSV with a header row. Required: account ID and company name. Recognized
headers (case/punctuation-insensitive), e.g. `Account #`, `Customer Name`, `DBA`,
`Address`, `City`, `State`, `Zip`, `Phone`, `ROC License`, `Branch`, `Rep`,
`Last Purchase Date`. Dates: `YYYY-MM-DD` or `M/D/YYYY` (two-digit years are not
guessed). Limits: 50 MB, 250,000 rows, 300 characters per field.

## Import guarantees

- **Provenance:** every import is a batch with ID, tenant, source file *name*,
  SHA-256, byte size, timestamp, row/accepted/rejected/warning counts.
- **Nothing dropped silently:** every row is accepted, rejected with a reason, or
  accepted with warnings → `import_issues.csv` (row number, level, reason, a
  masked preview such as `phone: '60…' (len 14)`).
- Rejected: missing account ID or name, duplicate account ID within the file (all
  copies — the tool does not guess which is right), over-long fields, control
  characters. Warnings: unparseable/future purchase date (status becomes unknown),
  unusable phone or license, nonstandard state.
- **Whole-file failures** (not UTF-8, NUL bytes, malformed CSV, missing required
  columns, ambiguous headers) write nothing.
- **All-or-nothing:** one transaction per file.
- **Snapshot semantics:** each import is the full current book; accounts absent
  from the newest file are kept but marked not-in-latest and excluded from matching.
- **Idempotent:** a byte-identical file is recognized by checksum and not re-imported.
- Source values are stored as provided; normalized matching keys are stored separately.

## Matching (shared intelligence, read-only)

Evidence: ROC license; phone (verified or candidate channel); street address +
ZIP/city (suite/unit and spelling variants normalized); exact company name or DBA
(suffixes, punctuation, "&/and", "The" normalized); high name similarity.

| State | Rule |
|---|---|
| VERIFIED | license matches + any corroboration; no conflicts; unique |
| HIGH_CONFIDENCE | exact name/DBA + phone or address; no conflicts; unique (or phone + address + similar name) |
| REVIEW_REQUIRED | any weaker evidence, any conflict (different license / verified phone / address in the same area), person-name-only, duplicate company names, or another company sharing the identifying detail |
| UNMATCHED | no candidate |

Name similarity alone never exceeds REVIEW_REQUIRED. Exact name + same city alone
is REVIEW_REQUIRED unless the policy flag `allow_name_city_high_confidence` is set.
No numeric scores are stored or shown.

## Review

`review.csv` lists every REVIEW_REQUIRED account with the supplier's values, the
candidate company, matching evidence, conflicting evidence, other candidates and
the reason. Fill `decision` with `CONFIRM`, `REJECT` or `SET_COMPANY` (+
`decided_company_id`). Decisions are stored in the tenant's `review_decisions`
table, persist across runs (latest wins), and override automated matches.

## Classification (Book × Market)

Only VERIFIED, HIGH_CONFIDENCE and human-confirmed matches are classified.

- Purchase status (supplier policy): ACTIVE if last purchase ≤ `active_purchase_days`;
  FORMER only if the supplier sets `former_after_days` and the purchase is older;
  otherwise DORMANT; no/invalid date → `UNKNOWN_PURCHASE_STATUS`.
- Market status: ACTIVE_MARKET if the matched company has ≥ `market_min_permits`
  permits in the last `market_window_days`; trend compares with the prior window.
- Classes: `{ACTIVE|DORMANT|FORMER}_CUSTOMER_{ACTIVE|QUIET}_MARKET`,
  `UNKNOWN_PURCHASE_STATUS`, `IDENTITY_REVIEW`, `UNMATCHED_ACCOUNT`.

Defaults (also used by the synthetic tests unless noted): active 90 days, former
off (tests also exercise 540 days), market window 90 days, minimum 1 permit,
name-review similarity 0.75.

## Net-new relevant contractors

CorridorIQ companies in the configured wet-side lanes (`PLUMBING_CORE`,
`FUEL_GAS_PROPANE`, `FIRE_BACKFLOW`, `CIVIL_WET_UTILITY`, `HVAC_MECHANICAL`) with
recent permit activity that do not confidently match any account. Companies that
appear as a candidate for an account under review are flagged as a possible
existing account. **Branch geography is not applied yet (future work).**

## Outputs

A new folder `<out>/<tenant_id>-<run>/` (never overwritten): `summary.md`,
`matches.csv`, `review.csv`, `classification.csv`, `net_new.csv`,
`import_issues.csv`. CSV cells are protected against spreadsheet formula injection.
These files are confidential to the supplier.

## Privacy and logging

Logs contain tenant ID, batch/run IDs, counts, timings and error categories only —
never names, phones, addresses, purchase dates or full rows. Never commit book
files or reports; keep them outside the repository.

## Backup and deletion

Back up `<tenant root>` alongside the shared database, keeping tenants separate and
encrypted off-machine. To delete a supplier's data: remove its tenant directory and
backups, then deactivate the tenant.

## Known limitations

- Most CorridorIQ companies have no phone on file and only ROC-linked companies have
  addresses, so many genuine matches will land in REVIEW_REQUIRED (by design).
- Permit activity reflects CorridorIQ's coverage and contractor attribution, which
  vary by jurisdiction and source.
- No branch geography, purchase-category history, or rep-level outputs yet.
- Fuzzy-name candidate scoring is the slowest step (~19 s for 5,000 accounts against
  the full company index); acceptable offline, not yet optimized.
- Account IDs are case-sensitive (`A-1` and `a-1` are different accounts).
