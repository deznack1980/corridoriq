# Integrated Release Readiness: Contractor/Supplier Onboarding

| | |
|---|---|
| Branch | `integration/onboarding-production` |
| Date | 2026-10-11 |
| Deploy authority | **None.** This document prepares a release; the owner performs it. |
| Companion | `PRODUCTION_RECONCILIATION.md` (commit-by-commit decisions) |

## Recommendation: **CONDITIONAL GO**

The code is ready for release. **The deploy must not start until conditions C-1 to C-6
below are complete.** C-1 (backup and restore) and C-2 (a live verification email) are
not yet satisfied, so **the status today is NO-GO.** Once both are done, it becomes GO
with C-3 to C-6.

| # | Condition before release | Owner | Status |
|---|---|---|---|
| C-1 | A backup of **every** data root has been taken immediately before the deploy, **and a restore of a recent backup has been demonstrated** in a disposable environment (§7, §10) | Owner | **Open** |
| C-2 | A **live verification email** has been delivered by Postmark from the production configuration to an owner-controlled mailbox, and the link verifies (§5) | Owner | **Open** |
| C-3 | Postmark: the sending domain or `noreply@corridoriq.pro` sender is verified, and DKIM shows verified (§5) | Owner | Open |
| C-4 | `CORRIDORIQ_CLIENT_IP_SOURCE=railway` checked on a non-production Railway service: audit-log addresses show real visitor IPs (§6) | Owner | Open |
| C-5 | The PR into the release branch is reviewed and approved (no self-merge without review), with the full test suite green | Owner + reviewer | Open |
| C-6 | Owner decisions D-1 to D-4 recorded (`PRODUCTION_RECONCILIATION.md` §8) | Owner | Open |

## 1. Verified production baseline

Production serves **`fcf50fe`**. Ten served files hash-match it, and no onboarding asset is
live. This branch starts from `fcf50fe`, so every production commit is retained.

## 2. Missing commits reconciled

- The onboarding branch lacked **23** production commits. All are present here by construction: base = production.
- Its 5 commits were ported feature by feature. The decision for each area is in `PRODUCTION_RECONCILIATION.md` §3–§4.
- Rejected outright:
  - the onboarding branch's separate token store, mailer, rate limiter and client-IP resolver;
  - its `auth_tokens` table definition;
  - its public mail diagnostics in `/api/health`;
  - its runbooks for the rejected designs.

## 3. Database compatibility results

The changes are additive only:
- four `users` columns (`account_kind`, `account_state`, `business_name`, `business_category`), with defaults that keep every existing row an employee;
- one index;
- new roles and permissions seeded by the existing seeder;
- one new token *purpose* in the existing `auth_tokens` table.

No column is dropped, renamed or retyped, and no existing row is rewritten.

**Cross-version harness** (`scripts/ops/cross_version_check.py`, `pipeline/tests/test_cross_version.py`): **54/54 checks passed.**

| Phase | What ran | Result |
|---|---|---|
| A | **Production code** (`git archive fcf50fe`) built a synthetic database with its own migrations and seed: admin and sales rep, live sessions, an outstanding password-reset token, a pilot supplier, and a pilot contractor with a sent material request | 5/5 |
| B | **Integrated code** on that database: the production session still works; production password hashes verify; the production reset token completes and revokes old sessions; the pilot session, login and request still work; contractor and supplier registration, verification and approval work; both are denied every employee route; suspension ends sessions; integrity ok | 30/30 |
| C | **Rollback**: production code on the *newer* database starts cleanly. Sessions created by integrated code, employee, admin and pilot logins all still work. Onboarding accounts can sign in but production still denies them CRM, admin, company and product data. Suspended accounts stay locked out. Integrity ok. | 19/19 |

The production `auth_tokens` schema conflict found in review is resolved: verification uses
production's `tokens.issue`/`tokens.consume` under the new purpose `portal_email_verify`.

## 4. Security issues fixed (relative to the onboarding branch)

| Issue in the onboarding branch | Resolution here |
|---|---|
| Missing 23 production commits (readiness gate, throttle, client-IP trust, hardened tokens, Postmark, Railway runtime) | Base is production |
| Email verification crashed on the production schema | Production token store and purpose |
| Tokens: no revocation on re-issue, non-atomic consume, not bound to the address | Production `tokens` (re-issue revokes; atomic conditional consume; bound to user and address; email change burns links) |
| SMTP `starttls()` without certificate verification | Strict SMTP adapter (verified chain and hostname, TLS 1.2+, mandatory STARTTLS or implicit TLS). Postmark HTTPS uses an explicit verifying context. |
| Silent fallback to an in-memory outbox in production | Production refuses to start with the sink or with an incomplete SMTP configuration |
| Public mail-mode disclosure in `/api/health` | Moved to the admin-only `/api/admin/mail-status` (booleans only) |
| Proxy trust accepted `CF-Connecting-IP` without a secret | Production secret-gated mode kept; a narrow Railway mode added (§6) |
| Registration and reset limits keyed on the raw TCP peer | Email-keyed buckets plus attributed client addresses; shared-hop limits |
| Verification via GET (mail scanners auto-verify; token in URL logs) | Tokens in the URL fragment; POST-only verification |
| Supplier approval could re-enable suspended accounts | Only `PENDING_SUPPLIER_APPROVAL → ACTIVE`; before and after state audited |
| Live RFQ and quote stubs answered "accepted" without storing anything | They answer `501 live_*_not_available` |
| New public writes without a JSON check | JSON required on every new public and admin write |

**Authorization:**
- Contractor and supplier accounts hold only namespaced permissions.
- `require_employee_workspace()` precedes every employee route.
- The admin role can never be self-registered (enforced in `create_user`).
- Supplier live access requires an address-bound verified email **and** admin approval.

## 5. SMTP / Postmark readiness

| Item | State | Evidence |
|---|---|---|
| Provider | Postmark HTTPS API (production default, unchanged). Strict SMTP available as `CORRIDORIQ_EMAIL_PROVIDER=smtp` (for example `smtp.postmarkapp.com:587`, STARTTLS). | Code + tests |
| TLS | Both transports verify the certificate chain and hostname, TLS ≥ 1.2. No plaintext or unverified fallback. | `test_smtp_*`, `test_postmark_transport_verifies_tls` |
| Fail-closed | Production refuses to start with the sink, a missing Postmark token, or incomplete SMTP. A failed send revokes the link and never claims delivery. | `readiness.py`, tests |
| Sender | `CorridorIQ <noreply@corridoriq.pro>` (override `CORRIDORIQ_MAIL_FROM`) | `mail.py` |
| Links | `CORRIDORIQ_PUBLIC_BASE_URL` (must be `https://` in production) + `/verify-email.html?mode=verify#t=…`; resets via production `login.html?mode=reset#t=…` | Tests |
| Return-Path / SPF | `pm-bounces.corridoriq.pro` CNAME `pm.mtasv.net`, which publishes `v=spf1 include:spf.mtasv.net -all`. DMARC `aspf=r` gives SPF alignment on the Return-Path subdomain. **The apex SPF does not need to change.** | Public DNS, 2026-10-11 |
| DKIM | **Not confirmed.** Postmark DKIM selectors are account-specific; copy the TXT record shown in Postmark → Sender Signatures/Domains into DNS (owner approval). This gives alignment that survives forwarding. | — |
| DMARC | `p=quarantine; adkim=r; aspf=r` (unchanged) | Public DNS |
| MX | Microsoft 365, **unchanged** | Public DNS |
| Bounces | Not processed by the app. Postmark suppresses hard bounces; a contractor with a mistyped address must contact support. | Gap (post-release) |

## 6. Proxy and rate-limit readiness

- **Production default (unchanged):** client address = TCP peer, unless the secret-gated Cloudflare mode is configured.
- **Railway mode** (`CORRIDORIQ_CLIENT_IP_SOURCE=railway`):
  - Uses `X-Real-IP` only from a non-public peer, and only when the value is a single public address. Railway documents that its edge sets this header ([Railway Specs & Limits](https://docs.railway.com/networking/public-networking/specs-and-limits)), and staff state the edge overwrites client copies ([Railway Station](https://station.railway.com/questions/which-client-address-headers-does-the-ra-fb9e36d7)).
  - No Railway IP range is hard-coded, because none is published.
  - `X-Forwarded-For` is never read.
- **Shared-hop safety net:** per-IP limits keyed on a private-network address (RFC 1918, carrier-grade NAT `100.64.0.0/10`, link-local, IPv6 unique-local), meaning a platform proxy hop rather than a visitor, are multiplied by 25. Email-keyed limits (registration 5, reset 5, resend 5, login failures 20 per 10 minutes) and account lockout keep the per-visitor controls.
- **New limits (per 10 minutes):**
  - registration: 10 per address, 5 per email
  - verification completions: 30 per address
  - resend: 10 per address, 5 per email
  - forgot/reset: production values
- **At startup**, production prints `WARNING: client_ip_unattributed` if neither Railway mode nor the Cloudflare hosted mode is configured.

**Verifying Railway mode (C-4):**
1. Deploy this branch to a **non-production** Railway service with `CORRIDORIQ_ENV=test` and `CORRIDORIQ_CLIENT_IP_SOURCE=railway`.
2. Sign in once from two different networks.
3. Confirm the two `security_audit_log.ip_address` values are those networks' public addresses, not `100.64.x.x`.

## 7. Backup dependency

The release adds columns and creates accounts on the production databases. Rollback does
not require a restore (§3 Phase C), but a deploy must never proceed without a fresh,
verified backup of **all** data roots:
- the main database (`CORRIDORIQ_DB_PATH` or `CORRIDORIQ_DATA_DIR`);
- the pilot platform (`CORRIDORIQ_PILOT_ROOT`, including every `contractor_tenants/*/contractor.db`);
- tenant stores (`CORRIDORIQ_TENANT_ROOT`).

`scripts/ops/backup_set.py` backs up every SQLite store consistently while the server runs
(`VACUUM INTO`), verifies each copy, writes a SHA-256 manifest, and restores only into an
empty, non-live folder. Its restore drill passes on synthetic data. **Condition C-1 requires
the same drill against a real production backup.**

## 8. Test results

| Suite | Result |
|---|---|
| Integrated onboarding (`test_onboarding_integrated.py`) | **59 passed** |
| Brand palette (`test_brand_palette.py`) | **6 passed** |
| Cross-version + rollback harness | **54/54 checks passed** |
| Full regression suite (`pipeline/tests` + `agents`, 1,346 tests) | **Clean re-run on the tip (`6baa849`): 1,345 passed, 1 failed** (16 min). The failure is the pre-existing, date-dependent CEO-agent test described below. First run: 1,331 passed, 14 failed. **2** were regressions from this branch, now fixed (shared-hop classification; the landing test asserted the retired amber). **12** were CEO-agent tests. See the correction below: only **1** is a genuine failure, and it is pre-existing. |
| Re-run after the fixes: affected and throttle-dependent suites (pilot, phase-2 identity/remediation/retest, billing v02, CRM security, tenant isolation, Postmark, landing, client address, onboarding, brand, cross-version) | **270 passed, 1 timed out**: `test_pilot_email_throttle_blocks_correct_password` hit an HTTP read timeout under CPU contention. It **passed 3 out of 3 when re-run alone** (about 7 seconds each), so it is not an assertion failure. Plus 147/147 for the directly affected files. |
| Production CI asset check (every local `src`/`href` exists) | Pass |
| `node --check` on changed JS | Pass |
| Real server entry point smoke test (loopback, synthetic data) | Pass: health, `robots.txt`, `sitemap.xml`, onboarding pages, public preview, supplier registration |

Not exercised: real Postmark delivery (C-2), Railway's real proxy headers (C-4), and browser
end-to-end flows of the new pages.

**Correction (2026-10-12, after Cursor's independent audit `2dd4fb3`):** the earlier count of 12 CEO-agent failures was wrong. 11 were caused by the author's test environment, which pointed `CEO_STATE_DIR` and `CEO_OUTPUT_DIR` at empty scratch folders. With a clean environment the three CEO files give **43 passed, 1 failed**, on both the integration tip and pristine `fcf50fe`. The remaining failure, `test_system_health.py::test_brief_generation_failure_keeps_the_prior_brief_and_is_critical`, is **pre-existing and date-dependent**: its fixture's last refresh is fixed at 2026-10-05, the path under test measures freshness against the real clock, and it now reports `warning`. Fix it separately (freeze the clock); it is unrelated to onboarding.


**Brand follow-up (non-blocking):** the raster share image (`assets/brand/corridoriq-social-1200x630.png`)
and touch icon (`corridoriq-touch-180.png`) still carry the retired amber palette. Regenerate them
from the gold SVG mark.

## 9. Remaining blockers

1. **C-1** backup and restore demonstration. **C-2** live verification email.
2. **C-3** Postmark sender/DKIM status. **C-4** Railway mode verification.
3. **C-5** review. The release branch has no branch protection, so the owner must enforce review manually.
4. **D-1** contractor identity. Public contractor calls to action stay on the live pilot sign-up. The new main-database contractor registration (`register-contractor.html`) is functional but unlinked until decided.

## 10. Rollback procedure

1. **Application rollback (preferred):** in Railway, redeploy the previous deployment (`fcf50fe`).
   - No database action is needed: the schema changes are additive, and production code was proven to start and serve the newer database (§3 Phase C).
   - Effects:
     - onboarding pages return 404;
     - onboarding accounts can still sign in at `login.html` but production denies them all CRM and admin data;
     - outstanding verification links stop working (fail closed);
     - `CORRIDORIQ_CLIENT_IP_SOURCE` is ignored by `fcf50fe`.
2. **Optional containment after rollback:** an admin disables onboarding accounts from user management (`is_active`), if wanted.
3. **Data rollback (last resort, only for corruption):**
   - restore the pre-deploy backup set **into a new volume / disposable service first**;
   - verify it (`backup_set.py verify`);
   - then switch. Restoring loses accounts created after the backup.
   - Never restore over the live volume without a fresh backup of its current state.

## 11. Exact production deployment steps (owner)

1. Complete C-1 … C-6.
2. Open a PR `integration/onboarding-production` → `release/corridoriq-2026-10-06`. Get the review, and confirm the full test suite is green locally (including `test_cross_version.py`, which needs the `fcf50fe` history).
3. **Railway variables** (production service; names only):
   - add `CORRIDORIQ_CLIENT_IP_SOURCE=railway`;
   - confirm `CORRIDORIQ_ENV=production`, `CORRIDORIQ_EMAIL_PROVIDER=postmark`, `POSTMARK_SERVER_TOKEN` (present), `CORRIDORIQ_PUBLIC_BASE_URL=https://corridoriq.pro`, `CORRIDORIQ_TRUSTED_PROXY_SECRET` (present), `CORRIDORIQ_PILOT_ROOT`;
   - confirm the DB path variables point inside the persistent volume.
4. **Pre-deploy backup:**
   - trigger a Railway volume backup (or snapshot) and record its timestamp;
   - after the deploy, also run `python scripts/ops/backup_set.py create --out <volume>/backups` in the service shell to start the all-stores backup history.
5. Merge the PR. Railway deploys the release branch.
6. **Post-deploy verification (within 15 minutes):**
   - `GET /api/health` → `ready: true`;
   - the deploy log has **no** `WARNING: client_ip_unattributed`;
   - `GET /robots.txt` and `/sitemap.xml` → 200;
   - an employee and a pilot contractor sign in;
   - register a supplier with an owner mailbox, then receive the email, verify, and approve it as admin;
   - the latest `security_audit_log.ip_address` shows the owner's public IP;
   - a new contractor or supplier gets 403 on `/api/sales/dashboard`.
7. If any step fails: rollback step 1, then investigate.
