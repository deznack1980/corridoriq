# Release Candidate Review Package: `integration/onboarding-production`

| | |
|---|---|
| Candidate | `integration/onboarding-production` (base `fcf50fe`, the verified production commit) |
| Target | `release/corridoriq-2026-10-06` (Railway production branch) |
| Author | Claude (lead engineer). **The author does not approve this release.** An independent reviewer and the owner must. |
| Companion docs | `PRODUCTION_RECONCILIATION.md`, `INTEGRATED_RELEASE_READINESS.md`, `RELEASE_GATE_REPORT.md` |

## 1. What changes

Contractor and supplier self-service onboarding, rebuilt on production's identity stack:
- registration and email verification;
- supplier approval and suspension;
- welcome workspaces, public preview, `robots.txt`/`sitemap.xml`;
- the approved brand palette;
- Railway client-address mode and shared-hop throttling;
- a strict SMTP option;
- ops tooling (backup, restore drill, cross-version harness, gate tests).

Commits since `fcf50fe`:

| Commit | Summary |
|---|---|
| `ba90ac3` | Onboarding rebuilt on production identity stack (backend) |
| `7884f45` | Front-end merge and approved brand palette |
| `cb4372b` | Integrated onboarding tests (59) |
| `ef676ec` | Cross-version/rollback harness; full-coverage backup tool |
| `ca964c6` | Reconciliation and readiness docs |
| `038b8bd` | Shared-hop classification fix; regression results |
| `cb4b4ec` | Restore drill; read-only Railway backup pull |
| `726c9f3` | Gate test tools (live email C-2, client IP C-4) |

## 2. Where to focus (security-critical)

| File | Review question |
|---|---|
| `pipeline/api/server.py` | Does every employee CRM/admin route sit **after** `onboarding.require_employee_workspace(user)` in both `_route_get` and `_route_write`? Are all new public writes in `_auth_json` (JSON-only) and throttled? Do the `_AUTH_ALIASES` map only onto existing production flows? |
| `pipeline/auth/onboarding.py` | Is the admin role impossible to self-assign? Is verification consumed atomically, bound to user and address, and rejected for suspended or non-network accounts? Is approval only `PENDING_SUPPLIER_APPROVAL → ACTIVE`? Does suspension revoke sessions and tokens? |
| `pipeline/auth/service.py` | `create_user` rejects `admin` for non-employee kinds. `get_current_user` rejects `SUSPENDED`. The context adds onboarding fields without changing employee behaviour. |
| `pipeline/api/client_address.py` | Railway mode trusts `X-Real-IP` **only** from a private-network peer **and** only a single public value. `X-Forwarded-For` is never read. The secret-gated Cloudflare mode is unchanged and takes precedence. |
| `pipeline/auth/throttle.py` | The shared-hop factor applies only to `_ip` buckets keyed on private-network addresses. Email buckets are unchanged. |
| `pipeline/auth/mail.py`, `readiness.py` | Both transports verify certificate and hostname (TLS 1.2+). SMTP never logs in without TLS. Production refuses the sink and any incomplete SMTP configuration. Diagnostics hold no secret values. |
| `pipeline/db/schema.sql`, `database.py`, `pilot/platform.py` | Additive columns only, defaults keep existing rows as employees, migrations idempotent and duplicate-safe |
| `pipeline/auth/rbac.py` | New roles hold only `contractor.*` / `supplier.*` permissions |

## 3. How to verify locally

Always use disposable data paths. On Windows the default data directory is a real one.

```
export CORRIDORIQ_ENV=test CORRIDORIQ_DATA_DIR=<tmp> CORRIDORIQ_DB_PATH=<tmp>/db/c.db \
       CORRIDORIQ_PILOT_ROOT=<tmp>/pilot CORRIDORIQ_TENANT_ROOT=<tmp>/tenants CORRIDORIQ_CACHE_WARMER=0
python -m pytest pipeline/tests/test_onboarding_integrated.py pipeline/tests/test_brand_palette.py \
       pipeline/tests/test_cross_version.py pipeline/tests/test_client_address.py -q
python scripts/ops/cross_version_check.py --production-ref fcf50fe     # 54 checks
python scripts/ops/client_ip_staging_check.py simulate                  # 7 checks
python scripts/ops/railway_backup.py selftest                           # backup → stream → restore
python scripts/ops/postmark_verification_test.py                        # dry run, sends nothing
python -m pytest pipeline/tests agents -q                               # full suite, ~2 h
```

## 4. Existing test failures (known, not introduced here)

| Tests | Status | Evidence |
|---|---|---|
| `agents/ceo/tests/test_system_health.py::test_brief_generation_failure_keeps_the_prior_brief_and_is_critical` | **Pre-existing, date-dependent.** It fails the same way on pristine `fcf50fe`. Earlier reports of 12 CEO failures were an artifact of the author's test environment (corrected) | Clean run: 43 passed, 1 failed on both the tip and `fcf50fe` |
| `test_phase2_retest.py::test_pilot_email_throttle_blocks_correct_password` | Intermittent HTTP read timeout under CPU load | Passes 3/3 in isolation |

## 5. Outstanding decisions (owner)

| ID | Decision | Recommendation |
|---|---|---|
| D-1 | Contractor CTAs: live pilot sign-up (current) vs new main-DB contractor registration | Keep the pilot sign-up until onboarding contractors can send live RFQs |
| D-2 | Mail provider | Postmark HTTPS API; strict SMTP only as a fallback |
| D-3 | Ship session-token hashing (local-only `integration/launch-readiness-v01`) | Push that branch now so it is preserved; release separately |
| D-4 | Enable `CORRIDORIQ_CLIENT_IP_SOURCE=railway` in production | Yes, after C-4 passes on staging |
| D-5 | Run the live C-2 email with the production Postmark server or a separate staging server | Production server (it validates the real sender/DKIM); one email to an owner mailbox |
| D-6 | Create a non-production Railway service for C-4 (may add usage cost) | Yes; delete it after the test |

## 6. Rollback requirements

- **Before deploy:** a verified backup of every data root (gate C-1), plus a Railway volume backup timestamp.
- **Application rollback:** redeploy `fcf50fe`. It is proven to start and serve the migrated database (cross-version phase C, 19/19); no restore is needed.
- **Data rollback** (corruption only): restore the pre-deploy set into a new volume or a disposable service first (`restore_drill.py`), then switch. Accounts created after the backup are lost.
- **Who can trigger it:** the owner. Expected application rollback time: one Railway redeploy.

## 7. Draft PR (to be opened by the owner, or by Claude with approval)

Title: `Onboarding: contractor/supplier self-service on the production identity stack (DRAFT, gated)`

Body:

```
Ports feature/frictionless-onboarding onto the verified production commit (fcf50fe).
See docs/engineering/PRODUCTION_RECONCILIATION.md and INTEGRATED_RELEASE_READINESS.md.

DO NOT MERGE until RELEASE_GATE_REPORT.md shows PASS for C-1..C-6.
Review focus: docs/engineering/RELEASE_REVIEW.md §2.
Author does not approve; an independent reviewer and the owner must.
```

Command (keeps it a draft): `gh pr create --draft --base release/corridoriq-2026-10-06 --head integration/onboarding-production --title "<title>" --body-file <file>`
