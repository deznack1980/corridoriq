# Release candidate — application security

**Branch:** `feature/frictionless-onboarding`  
**Not merged. Not deployed.**  
**Brand tokens:** unchanged amber-gold / charcoal.

## Test results (this execution)

312 passed in 233s:

- `test_release_security`
- `test_brand_tokens`
- `test_launch_readiness`
- `test_frictionless_onboarding`
- `test_option_b_safety`
- `test_sales_crm_security`
- `test_tenant_isolation`
- `test_sales_gate`
- `test_trust_portal`

Previous launch/brand baseline was 194. CRM/isolation suites (105) plus the new security tests account for the rest.

## Security fixes in this candidate

1. **Rate-limit IP isolation (confirmed).** `_client()` used only the
   TCP peer. Behind Railway that is one shared address, so one abusive
   client could exhaust registration/reset quotas for everyone.
   - Forwarded headers are ignored unless `CORRIDORIQ_TRUST_PROXY=1`
     and the peer is in `CORRIDORIQ_TRUSTED_PROXIES`.
   - Cloudflare `CF-Connecting-IP` is preferred over `X-Forwarded-For`.
   - Per-email buckets sit on top of per-IP buckets. Limits were not
     relaxed.
   - Reset-password is now rate-limited (same hourly cap as forgot).
2. **Suspended sessions.** `get_current_user` rejects `SUSPENDED` even
   if a cookie remains. Suspend already sets `is_active=0` and revokes
   sessions; password reset cannot revive a suspended account.
3. **Additive migrate.** `ALTER TABLE` duplicate-column races are
   ignored; other migration errors still fail closed.

## Email

SMTP is still **unconfigured** on the development workstation
(`delivery_mode: outbox`). Verification-dependent production mail is a
**release blocker** until staging credentials are installed. See
`docs/operations/smtp_staging.md`. Tests use the in-memory outbox only.

## Migrations

`migrate_schema` adds `users.account_kind`, `account_state`,
`email_verified_at`, `business_name`, `business_category` with employee /
ACTIVE defaults. `auth_tokens` is `CREATE TABLE IF NOT EXISTS`.
Existing emails, hashes, and roles are preserved. Idempotent.

**Backup:** confirm Claude's off-site backup check before production
apply. This candidate does not touch production data.

## SEO

`GET /robots.txt` and `GET /sitemap.xml` return 200 with
`https://corridoriq.pro` URLs. Private routes stay out of the sitemap.
Do not submit Search Console until production serves these files.

## Remaining risks

| Risk | Status |
| --- | --- |
| SMTP not installed | Blocker for live verification/reset mail |
| `TRUST_PROXY` not set on Railway | Blocker for fair rate limits in production |
| Origin reachable besides the proxy | Would allow header spoofing if trust is on |
| In-process limiter | Per-process; multiple replicas do not share counters |
| SOC 2 / infra items | Owned by Claude's branch — not changed here |

## Rollback

Revert this commit on `feature/frictionless-onboarding`. No schema
downgrade is required (columns are additive and unused columns are
harmless).

## Deployment steps (administrator, after review)

1. Backup the production SQLite file.
2. Merge only after independent security review (not this agent).
3. Set `CORRIDORIQ_ENV=production`, `CORRIDORIQ_PUBLIC_URL=https://corridoriq.pro`.
4. Set SMTP secrets and `CORRIDORIQ_TRUST_PROXY=1` +
   `CORRIDORIQ_TRUSTED_PROXIES=loopback,private`.
5. Restart the portal. Confirm `/api/health`, `/robots.txt`, `/sitemap.xml`.
6. Smoke: contractor register/verify, supplier register/approve, employee login.
7. Do not change billing, pricing, Cloudflare, or Railway topology in this step
   except the documented env vars.
