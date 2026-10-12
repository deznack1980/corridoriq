# P0: Contractor Signup, "Check Your Email" but No Email Arrives

| | |
|---|---|
| Branch | `hotfix/signup-delivery` (based on production `fcf50fe`) |
| Date | 2026-10-12 |
| Deploy authority | None. The owner approves and performs any deploy. |

## 1. Verified facts

| Question | Answer | Evidence |
|---|---|---|
| Production commit | **`fcf50fe`** (release head) | Live `contractor-account.html`, `pilot-common.js`, `login.html` and `landing.html` are byte-identical to `fcf50fe` |
| Who serves the signup page | Railway (`Server: railway-hikari`), **production pilot flow**, not the unreleased onboarding branch | Hash match; `/api/pilot/session` answers `{"signed_in": false}` |
| Endpoint | `POST /api/pilot/auth/signup` → `accounts.request_contractor_signup` (pilot platform database) | `fcf50fe` source |
| Mail configuration present | Yes: the production readiness gate passes, so `CORRIDORIQ_EMAIL_PROVIDER=postmark`, a non-empty `POSTMARK_SERVER_TOKEN` and an https base URL are set | `/api/health` → `ready: true` |
| Postmark DNS | Return-Path `pm-bounces.corridoriq.pro` CNAME `pm.mtasv.net` ✓. **No Postmark DKIM record found** at guessable selectors (the real selector is visible only in Postmark). | Public DNS |

## 2. Root cause of the *symptom* (verified in code)

`fcf50fe` answers `{"status": "check_email"}`, and the page shows the confirmation, **in all of these cases**:

| Path | What happens | Server log | Audit (`pilot platform.db`, `email_verification_requested`) |
|---|---|---|---|
| Postmark accepted | Email sent | nothing | `result: sent` |
| **Postmark rejected or unreachable** (account pending approval, sender/domain not verified, inactive recipient, bad token, network) | **Nothing sent**; the link is revoked | **nothing** | `result: mail_unavailable` |
| **Address already has an account** | **Nothing sent** | nothing | `result: already_registered` |
| **Same address tried more than 5 times in 10 minutes** | **Nothing sent**; the signup never runs | nothing | *no row* |

Neither `mail.py` nor the signup code writes any log line. **Railway logs therefore cannot show this failure**, and the failure is invisible to both user and operator.

## 3. The specific failure: confirm with one of these checks (5 minutes, read-only)

The author has no Railway or Postmark access. The decisive evidence is in the production audit log and Postmark.

1. **Postmark console:**
   - Is the account **pending approval**? Until approved, Postmark accepts only recipients on the sender's own domain, so Gmail is refused (API ErrorCode 412).
   - Sender Signatures/Domains: is `corridoriq.pro` (or `noreply@corridoriq.pro`) **verified**, with DKIM verified? If not, ErrorCode 400/401.
   - Activity: is there any message to the Gmail address? Suppressions: is that address listed (ErrorCode 406)?
2. **Production audit (read-only):** `python scripts/ops/signup_delivery_diagnostic.py command <gmail address>` prints a `railway ssh` command. It outputs only timestamps, result codes, a yes/no for an existing account, and 24-hour totals per result.

| Diagnostic shows | Cause | Fix (no code needed) |
|---|---|---|
| `mail_unavailable` | Postmark refused the send | Request account approval / verify the sender signature or the domain DKIM (add the TXT record Postmark shows; DNS change needs owner approval) / remove the suppression |
| `already_registered` | Address already registered | Sign in or use "Forgot password" at `contractor-account.html?mode=signin`; test with a fresh address |
| no rows | Per-email throttle (repeated tests) or the request never reached production | Wait 10 minutes; use a fresh address |
| `sent` | Postmark accepted it | Postmark Activity shows the delivery result; check Gmail filters/blocks |

**Most likely**, given no visible DKIM record and this being the first external send: **Postmark account approval or sender/domain verification** (`mail_unavailable`). It is **not confirmed** until one of the checks above is done.

## 4. Repair the existing signup, or ship the integrated onboarding release?

**Repair the existing production signup.** If the cause is Postmark configuration, no deploy is needed at all. The hotfix makes any remaining delivery failure visible and fixes two UX defects. It is small:
- 7 files, no schema change;
- rollback is a redeploy of `fcf50fe`.

The integrated onboarding release has six blocked gates and does not change this pilot flow.

## 5. Hotfix contents (`hotfix/signup-delivery`)

- `mail.send` logs one stdout line per identity email:
  - `MAIL_SENT kind=… provider=…`; or
  - `MAIL_DELIVERY_FAILED kind=… reason=postmark send rejected (HTTP 422, ErrorCode 412) hint=account pending approval…`
  - It never logs the recipient, link, token or provider message text.
- Postmark HTTPS uses an explicit verifying TLS context. Error bodies are read only for the numeric ErrorCode.
- The signup audit stores the sanitized `mail_error`.
- An address that already has an account receives a **no-token "already registered" notice** with sign-in and reset links, instead of nothing. The public answer is unchanged, so there is no enumeration.
- The silent per-email throttle logs `SIGNUP_THROTTLED`.
- **Shared-IP fix:**
  - opt-in `CORRIDORIQ_CLIENT_IP_SOURCE=railway` trusts `X-Real-IP` only from a private-network peer;
  - per-IP limits keyed on a platform hop are scaled ×25, so one visitor (or ten contractors) cannot exhaust the platform-wide signup limit;
  - startup warns when no client-address source is configured.
- **Tools:**
  - `scripts/ops/signup_delivery_diagnostic.py` (read-only);
  - `scripts/ops/pilot_signup_live_test.py` (dry run by default; one real email only with `--approved --to`).

## 6. Test results

| Suite | Result |
|---|---|
| `test_signup_delivery_hotfix.py` | **17 passed**: full journey (signup → email → link → password → logout → login → contractor portal request), Postmark rejection logged and audited with the link revoked, unreachable provider logged, already-registered notice, throttle logged, supplier inbox and admin access unaffected, verified TLS, client-address and shared-hop cases |
| `pilot_signup_live_test.py` dry run | **6/6 PASS** (signup, handed to provider, link completes, sign-in, session, portal) |
| Production regression subset (pilot, phase-2 identity/remediation/retest, Postmark, client address, CRM security, tenant isolation, billing ×3, landing, trust portal) | **498 passed, 0 failed** (11 min) |
| Full suite (`pipeline/tests` + `agents`, clean environment, `d72152e`) | **1,296 passed, 1 failed** (26 min). The one failure is `agents/ceo/tests/test_system_health.py::test_brief_generation_failure_keeps_the_prior_brief_and_is_critical`. It is pre-existing and date-dependent: its fixture's last refresh is fixed at 2026-10-05, so it now reports `warning`, and it fails identically on `fcf50fe`. It is unrelated to signup. |

Not yet exercised: a real Postmark send (needs owner approval, A-2) and Railway's real proxy headers (staging, A-4).

## 7. Fastest safe path

1. **Now (owner, read-only, about 5 minutes):** run the §3 checks.
2. **Fix the configuration it reveals** (Postmark approval / sender / DKIM / suppression). Then re-test on the live site **with a fresh address**. If the email arrives, **contractor registration works today with no deploy.**
3. **Then ship the hotfix:**
   - verify production backups first (`scripts/ops/railway_backup.py` + `restore_drill.py`, on the integration branch);
   - Cursor's independent review (§9);
   - owner approval;
   - PR → `release/corridoriq-2026-10-06`; Railway deploys.
   - Optionally set `CORRIDORIQ_CLIENT_IP_SOURCE=railway` after a staging check.
4. **Post-deploy:** one signup with a fresh address, then confirm a `MAIL_SENT kind=pilot_signup_verification` line in the Railway logs and the email in the inbox.

**Rollback:** redeploy `fcf50fe`. There are no schema or data changes.

## 8. Remaining blockers

- No Railway or Postmark access for the author: the exact cause is unconfirmed (§3).
- Production backup and restore not yet demonstrated (tools ready; owner approval A-1).
- Cursor review pending.
- A real email test needs owner approval and a mailbox.

## 9. Review request for Cursor (independent security review)

Please review `fcf50fe..hotfix/signup-delivery`:

- `pipeline/auth/mail.py`:
  - no recipient, link, token or provider message text in logs or exceptions;
  - only the numeric ErrorCode is parsed from error bodies;
  - the TLS context is verifying.
- `pipeline/pilot/accounts.py`: the already-registered notice carries no token; the public response is unchanged (no enumeration); the audit adds only sanitized `mail_error`.
- `pipeline/pilot/api.py`: the throttle log line has no address.
- `pipeline/api/client_address.py` / `throttle.py`:
  - Railway mode trusts `X-Real-IP` only from a private-network peer, and only a single public value;
  - `X-Forwarded-For` is never read;
  - the secret-gated Cloudflare mode is unchanged;
  - the ×25 factor applies only to `_ip` buckets keyed on platform hops.
- How to run the tests: `python -m pytest pipeline/tests/test_signup_delivery_hotfix.py` with disposable `CORRIDORIQ_*` data paths (`CORRIDORIQ_ENV=test`).
