# Final launch readiness

**Branch:** `feature/frictionless-onboarding`  
**Date:** 2026-10-10  
**Recommendation:** **CONDITIONAL GO**

Not merged. Not deployed. Brand palette unchanged.

---

## A. SMTP status

| Check | Status |
| --- | --- |
| Application SMTP host / user / password | **NOT CONFIGURED** |
| Postmark (or other app relay) | **NOT CONFIGURED** |
| Sender default in code | `CorridorIQ <noreply@corridoriq.pro>` |
| `CORRIDORIQ_PUBLIC_URL` on this workstation | **NOT CONFIGURED** (local default) |
| Templates (verify / resend / reset) | **CONFIGURED** — plain text, outbox-tested |
| Single-use and expiry | **CONFIGURED** — automated tests |
| SPF (`corridoriq.pro`) | **CONFIGURED** for `secureserver.net` only, `-all` |
| DKIM | **CONFIGURED** for Microsoft 365 `selector1`; **NOT CONFIGURED** for Postmark |
| DMARC | **CONFIGURED** (`p=quarantine`) |
| MX | Microsoft 365 |
| Live customer send from this branch | **NOT TESTED** (no approved relay credentials) |

**Owner action:** choose a relay that SPF already allows (GoDaddy/secureserver or Microsoft SMTP AUTH), or add a new include to SPF **before** pointing the app at Postmark. Set `CORRIDORIQ_SMTP_*` and `CORRIDORIQ_PUBLIC_URL=https://corridoriq.pro` in the host secret store. Send only to an operator inbox.

## B. Proxy-trust status

**Do not enable `CORRIDORIQ_TRUST_PROXY=1` yet.**

Public HTTPS identifies Railway (`railway-hikari`). Cloudflare is the DNS
host but is **not** on the HTTP path (no `cf-ray`).
`CORRIDORIQ_TRUSTED_PROXIES=loopback,private` is too broad.

Keep trust off. After the owner proves the origin accepts only Railway,
set an explicit `/32` hop. See `docs/operations/proxy_trust.md`.

Shared-edge IP buckets remain (8 registrations / hour). Per-email
buckets still apply.

## C. Backup status

| Copy | Status |
| --- | --- |
| Local verified backups | **VERIFIED** — 14 files, newest 2026-10-10 21:48:19Z, integrity `ok`, 1,402,765 rows |
| Off-site R2 | **UNVERIFIED** — credentials present; TLS handshake to R2 failed (known class of failure already documented). No replica list was retrieved. |

Local backups exist. Off-site replication is **not** confirmed from this run.

## D. Restore-test status

**VERIFIED** against a disposable copy of the newest local backup
(not written over the live file):

- `PRAGMA integrity_check` = `ok`
- `users`, `roles`, `sessions`, `organizations` present
- `users` row count read as an aggregate only

## E. Staging test results

No authorized Railway staging deploy was performed.

Local staging-equivalent (temp SQLite + Playwright) covers homepage,
explore, registration, welcome dashboards, login, reset, robots, and
sitemap. Screenshots: `docs/operations/brand-screenshots/after/` and
`docs/operations/launch-screenshots/`.

Production `https://corridoriq.pro/robots.txt` and `/sitemap.xml`
currently return **404** because this branch is not what Railway is
serving. `/` and `/login.html` return 200.

## F. Security regression

Executed 2026-10-10 on this working tree: **314 passed** in 164s
(release security, rollback compatibility, brand tokens, launch,
onboarding, Option B, CRM security, tenant isolation, sales gate,
trust portal). Prior baseline was 312.

## G. Production configuration requirements

Must be set on the host before promising verification mail:

- `CORRIDORIQ_ENV=production`
- `CORRIDORIQ_PUBLIC_URL=https://corridoriq.pro`
- SMTP host, user, password, from
- Persistent `CORRIDORIQ_DB_PATH` / data dir (already used by backups)

Must **not** be set until origin isolation is proven:

- `CORRIDORIQ_TRUST_PROXY`
- `CORRIDORIQ_TRUSTED_PROXIES=private`

Secure cookies flip on with `CORRIDORIQ_ENV=production`. Sessions are
already HTTP-only / SameSite=Lax.

## H. Rollback readiness

**VERIFIED** on a disposable database: a contractor created after
migrate remains readable using only pre-onboarding columns.

Live rollback = redeploy the previous application artifact. Do not
restore an older database over a newer one. Full procedure:
`docs/operations/production_release_runbook.md`.

## I. Remaining blockers

1. SMTP not installed — verification/reset mail will stay in the outbox.
2. SPF `-all` will reject a new relay until DNS is updated by the owner.
3. Off-site R2 replication not confirmed (TLS failure).
4. This branch is not deployed — production still 404s robots/sitemap.
5. Proxy trust must stay off until the owner confirms Railway-only ingress.

## J. Owner approvals needed

1. Approve this branch for Railway after independent security review.
2. Install SMTP and, if needed, update SPF/DKIM (DNS) — not this agent.
3. Accept residual off-site backup risk **or** repair R2 TLS.
4. Confirm Railway origin is not publicly reachable before any trust flag.
5. Authorize a synthetic-inbox mail test.

---

## Recommendation

**CONDITIONAL GO** for Monday outreach **only if** items J1–J4 are
accepted or completed. Application auth, isolation, additive migrate,
local backups, and rollback compatibility are in place.

**NO-GO** for promising live verification email until SMTP + SPF match.

Do not sacrifice those gates to the October 12 date.
