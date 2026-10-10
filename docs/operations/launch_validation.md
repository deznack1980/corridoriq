# Phase 2 — Launch validation executive report

**Project:** Corridor IQ  
**Branch:** `feature/frictionless-onboarding`  
**Baseline:** `8d335b1`  
**Target:** Monday, 12 October 2026  
**Status:** Ready for administrator approval. **Not merged. Not deployed.**

---

## 1. Completed improvements

- SMTP audit: diagnose-only status, redacted disk outbox, no credentials in logs or health.
- Contractor welcome now states the value in one sentence, walks a synthetic BOM → quote path, and labels **Available now / Demonstration / Locked**.
- Supplier welcome shows the three-state path. Pending accounts cannot read RFQs, contacts, or employee CRM.
- Public `robots.txt` and `sitemap.xml` always use `https://corridoriq.pro` and exclude private routes.
- Health reports mail configured/mode only. `GET /api/admin/mail-status` is `admin.system` only.

## 2. Files modified or added

| Area | Files |
| --- | --- |
| Mail | `pipeline/auth/mailer.py`, `pipeline/auth/mail.example.env`, `docs/operations/smtp_staging.md` |
| Contractor / supplier UX | `contractor-welcome.html`, `contractor-welcome.js`, `supplier-welcome.html`, `supplier-welcome.js`, `auth-public.css`, `pipeline/auth/onboarding.py` |
| SEO | `pipeline/api/seo.py`, `robots.txt`, `sitemap.xml`, `docs/operations/google_search_console.md` |
| Server / settings | `pipeline/api/server.py`, `pipeline/config/settings.py` |
| Token pages | `verify-email.html`, `reset-password.html` (`noindex`) |
| Tests / shots | `pipeline/tests/test_launch_readiness.py`, `pipeline/tests/test_option_b_safety.py`, `pipeline/reports/launch_screenshots.py`, `docs/operations/launch-screenshots/` |

CEO agent and contractor-pilot dirty files were left untouched.

## 3. Test results

All run against throwaway SQLite databases. Production data was not opened.

| Suite | Result |
| --- | --- |
| `test_launch_readiness` + `test_frictionless_onboarding` + `test_option_b_safety` | **190 passed** |
| `test_sales_crm_security` + `test_tenant_isolation` + `test_sales_gate` + `test_trust_portal` | **105 passed** |
| Playwright launch screenshots (temp DB) | **11 pages captured** |

Covered: contractor register/login, supplier register/verify/approve, resend + password reset, employee login, admin isolation, session `HttpOnly`/`SameSite=Lax`, protected endpoints, public pages, mobile nav CSS (44px targets), SEO files.

## 4. Screenshots

Saved under `docs/operations/launch-screenshots/`:

- `home-desktop.png`, `home-mobile-nav.png`
- `explore-desktop.png`
- `register-contractor-desktop.png`, `register-contractor-mobile.png`
- `register-supplier-desktop.png`
- `login-desktop.png`, `login-mobile.png`
- `reset-password-desktop.png`
- `contractor-welcome-desktop.png` — value, BOM demo, locked live RFQs
- `supplier-welcome-desktop.png` — verification + approval path, locked RFQs/contacts

## 5. Email delivery status

**Not live on this workstation.** `python -m pipeline.auth.mailer diagnose` reports:

- `smtp_configured`: false
- host / user / password: unset
- `delivery_mode`: outbox
- `public_url`: `http://127.0.0.1:8780` (local default; production must set `CORRIDORIQ_PUBLIC_URL=https://corridoriq.pro`)

Registration, verification, resend, and reset **logic is tested** via the in-memory outbox. Real SMTP requires administrator credentials on the staging host. See `docs/operations/smtp_staging.md`. No provider was purchased.

## 6. Supplier approval validation

| Step | Result |
| --- | --- |
| Self-register | `PENDING_EMAIL_VERIFICATION`, session issued, welcome only |
| Before verify | RFQs, quotes, contacts, commercial → **403** |
| After verify, before approve | State `PENDING_SUPPLIER_APPROVAL`; live routes still **403** |
| Contractor cannot approve | **403** |
| Admin approve | State `ACTIVE`; RFQs/contacts **200** |
| Approved supplier still blocked from employee CRM | `/api/sales/dashboard` **403** |
| Never auto-approved | Confirmed |

## 7. SEO readiness

- `GET /robots.txt` and `GET /sitemap.xml` served from `pipeline/api/seo.py`.
- Sitemap origin is always `https://corridoriq.pro` (seven public URLs).
- Disallows `/api/`, employee dashboards, welcome workspaces, verify/reset, join links.
- Welcome and token pages send `noindex`.
- Search Console steps: `docs/operations/google_search_console.md`. Submit only after production serves these files.

## 8. Remaining launch blockers

1. **SMTP credentials** must be installed on staging, then a real inbox test run (register, verify, resend, reset).
2. **Administrator approval** of this branch before any production deploy.
3. **Search Console** submission after production is live with the new crawl files.
4. Confirm production env: `CORRIDORIQ_ENV=production` (Secure cookies) and `CORRIDORIQ_PUBLIC_URL=https://corridoriq.pro`.

Not blockers: billing/pricing (unchanged), employee accounts (preserved), SOC 2 work (not touched).

## 9. Exact production deployment requirements

Do **not** merge or deploy from this report. When an administrator approves:

1. Merge `feature/frictionless-onboarding` through the normal review path (not this agent).
2. Apply additive schema (`CREATE TABLE IF NOT EXISTS` + `migrate_schema`). Do not replace the production database.
3. Set host secrets: `CORRIDORIQ_SMTP_HOST`, `CORRIDORIQ_SMTP_USER`, `CORRIDORIQ_SMTP_PASSWORD`, `CORRIDORIQ_SMTP_FROM`, `CORRIDORIQ_PUBLIC_URL=https://corridoriq.pro`, `CORRIDORIQ_ENV=production`.
4. Restart the portal process. Confirm `GET /api/health` shows `mail.configured: true` and `GET /robots.txt` + `GET /sitemap.xml` on `corridoriq.pro`.
5. Smoke: contractor register → welcome → verify; supplier register → verify → admin approve; employee login; forgot-password.
6. Submit the sitemap in Google Search Console.
7. Do not change billing, Cloudflare, or Railway settings in this release unless a separate operator task says so.
