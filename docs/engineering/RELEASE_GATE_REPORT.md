# Release Gate Report: Onboarding Release Candidate

| | |
|---|---|
| Candidate | `integration/onboarding-production` (see `RELEASE_REVIEW.md` for the commit list) |
| Production | `fcf50fe`, unchanged (release head and live files re-verified) |
| Date | 2026-10-12 |
| Overall | **NO-GO. 0 PASS · 0 FAIL · 6 BLOCKED.** Nothing failed; every gate is waiting on access, approval or review the author cannot provide. |

## Summary

| Gate | Status | Blocked on |
|---|---|---|
| C-1 Backup + restore of current production | **BLOCKED** | Railway access (none in this environment) |
| C-2 Live verification email via Postmark | **BLOCKED** | Owner approval, test mailbox, Postmark token for one run |
| C-3 Postmark domain, DKIM, Return-Path verified | **BLOCKED** (Return-Path ✓ in DNS) | Postmark console access; DKIM not visible in DNS |
| C-4 Railway client-IP mode on staging | **BLOCKED** (local emulation ✓) | A non-production Railway service |
| C-5 Independent review | **BLOCKED** | A named reviewer; the author does not approve |
| C-6 Owner decisions | **BLOCKED** | D-1 … D-6 (`RELEASE_REVIEW.md` §5) |

## C-1 — Backup and restore (BLOCKED)

**Ready to execute.** `scripts/ops/railway_backup.py`:
- `inventory` reports every database, its mount point (volume or not), sizes and journal mode; no row data.
- `stream` takes `mode=ro` + `VACUUM INTO` copies into the container's temporary disk and never writes to the volume. It does not block writers.
- Locally, `receive` verifies the SHA-256, then `restore_drill.py` restores into a disposable folder, checks integrity and core tables, reports counts only, applies the release's migrations and deletes the copy.

**Evidence so far:**
- `railway_backup.py selftest`: **PASS**. Inventory found 4 databases (main, pilot platform, contractor registry, one contractor store); stream → checksum → restore drill all pass.
- Restore drill on the **newest real backups available** (2026-10-06 pre-cutover main + pilot, opened read-only, copies deleted):
  - **5/5 checks pass**: integrity ok, core tables present, release migrations apply.
  - Timings: backup 45 s, verify 22 s, restore 21 s, inspect 29 s, migrate 32 s, so **about 2.5 min for about 580 MB**.
  - Aggregates: 4 users, 13,090 companies, 117,396 permits, 1 pilot supplier, 0 contractor profiles, 0 RFQs.
  - This is **not** C-1: those backups predate production's current data.
- **Recovery-time estimate:** data steps take about 3 minutes; the total is dominated by Railway operations (new volume or service plus redeploy), which are untested.

**Remaining gaps:**
- the Railway volume backup schedule is unknown;
- the off-site copy target is undecided;
- the in-container transfer over `railway ssh` has not been exercised on Railway (its checksum detects corruption).

**Approval required (A-1):** *"Approved: run `railway_backup.py` inventory and stream against production service `<name>` (read-only; writes only to container temp disk)."* The owner must also run `railway login` on this machine, or run the two printed commands personally (`python scripts/ops/railway_backup.py command inventory|stream`).

## C-2 — Live verification email (BLOCKED)

**Ready to execute.** `scripts/ops/postmark_verification_test.py`:
- runs the release on loopback with a disposable database;
- registers a synthetic supplier for the owner's mailbox and sends **exactly one** email;
- waits for the link to be clicked on this machine.

**Evidence so far:** the dry run passes (registration, fragment link, verification). The guards refuse to send without both `--approved` and `--to`, and without an operator-set token. Nothing has been sent.

**Approval required (A-2):** *"Approved: send one verification email to `<mailbox>` via Postmark server `<name>`."* The owner sets `POSTMARK_SERVER_TOKEN` in the shell for that run. It is never printed.
- Run: `python scripts/ops/postmark_verification_test.py --approved --to <mailbox>`.
- **PASS** = the link verifies the account and the message source shows `spf=pass`, `dkim=pass (d=corridoriq.pro)`, `dmarc=pass`.

## C-3 — Postmark configuration (BLOCKED)

| Item | Status | Evidence |
|---|---|---|
| Return-Path | ✓ in DNS | `pm-bounces.corridoriq.pro` CNAME `pm.mtasv.net` (SPF `include:spf.mtasv.net -all`) |
| SPF alignment | ✓ by design | DMARC `aspf=r`. **The apex SPF does not need to change.** |
| DKIM | **Not confirmed** | No Postmark DKIM record at guessable selectors; the real selector is shown only in Postmark |
| Sending domain / sender signature | Unknown | Postmark console |
| MX (Microsoft 365) | Unchanged | DNS |

**Owner action (A-3):** in Postmark → Sender Signatures/Domains → `corridoriq.pro`, record the Domain, DKIM and Return-Path status.
- If DKIM is pending, the required record is a TXT at `<selector>._domainkey.corridoriq.pro` with the value Postmark shows.
- **Approval required:** *"Approved: add that TXT record in Cloudflare."* No other DNS change is needed.

## C-4 — Railway client-IP mode (BLOCKED)

**Evidence so far:** `client_ip_staging_check.py simulate` passes **7/7** against an emulation of Railway's documented edge. Visitor A with forged `X-Real-IP` / `X-Forwarded-For` / `CF-Connecting-IP` headers:
- is limited at the 11th request;
- visitors B and C are unaffected;
- A's login lockout does not block B;
- the audit log holds only real client addresses.

The mode-off control shows every visitor sharing the hop. Unit tests cover the resolver (11 cases) and throttle classification.

**To execute:**
1. Create a non-production Railway service from this branch with `CORRIDORIQ_ENV=test`, `CORRIDORIQ_CLIENT_IP_SOURCE=railway`, `CORRIDORIQ_EMAIL_PROVIDER=sink`, and no production data or secrets.
2. Run `remote --phase spoof` from network A.
3. Run `remote --phase independent` from network B within 10 minutes.

**Approval required (A-4):** *"Approved: create staging service `<name>` (may add Railway usage cost); delete after the test."*

## C-5 — Independent review (BLOCKED)

Package: `RELEASE_REVIEW.md`, covering focus files, verification commands, known failures, decisions and rollback.

Test evidence:
- 59 onboarding;
- 6 brand;
- cross-version 54/54 (including rollback);
- full suite: 2 branch regressions fixed. Only **1** genuine CEO-agent failure, pre-existing and date-dependent (the earlier count of 12 was a test-environment artifact, corrected after Cursor's audit). A clean full re-run on the tip is in progress.
- **Independent audit complete (Cursor, `2dd4fb3`):**
  - reproduced 59 onboarding, 54/54 cross-version and 6 brand tests on `038b8bd`;
  - **no authorization bypass found**;
  - `038b8bd..bd1fac7` contains **no product-code changes** (ops scripts and docs only).
  - The audit is not a release approval. The PR review/approval remains with the owner.

**Approval required (A-5):** name the reviewer. *"Approved: open the draft PR"*, or the owner opens it with the command in `RELEASE_REVIEW.md` §7.

## C-6 — Owner decisions (BLOCKED)

D-1 to D-6 are in `RELEASE_REVIEW.md` §5, each with a recommendation. **A-6:** record a decision for each.

## Rollback requirements (unchanged)

- Pre-deploy backup of all data roots (C-1).
- Application rollback = redeploy `fcf50fe`. Proven compatible with the migrated database (cross-version phase C), with no restore needed.
- A data restore goes into a new volume or a disposable service first.
