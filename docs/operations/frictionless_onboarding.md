# Frictionless onboarding — launch readiness

Branch: `feature/frictionless-onboarding`. Not merged. Not deployed.

## A. Current authentication architecture (after this work)

The production portal (`pipeline/api/server.py`) remains the employee CRM
plus new public-network accounts. Sessions are server-side, HTTP-only
cookies. Passwords are Argon2id/scrypt hashes. Tokens are SHA-256 hashes
in `auth_tokens`.

| Audience | How the account is created | Landing page |
| --- | --- | --- |
| Employee (admin, sales, estimator, …) | Admin CLI / user-management only | Existing role dashboards |
| Contractor | `POST /api/auth/register/contractor` | `contractor-welcome.html` |
| Supplier | `POST /api/auth/register/supplier` | `supplier-welcome.html` |

The contractor **pilot** (`/api/pilot/*`, `/join/<code>`) is unchanged and
still uses its own database and cookie. This work does not route public
signup through the pilot.

## B. Problems discovered

1. Public pages asked visitors to email Archie or request a walkthrough.
   There was no contractor or supplier self-registration on the main portal.
2. `login.html` was an employee-only gate with no signup or password reset.
3. Email verification did not exist. The pilot exposed
   `email_verification: pending` / `PENDING_NOT_IMPLEMENTED`.
4. Password reset did not exist.
5. Contractor and supplier were not roles on the main RBAC catalog.
6. Sample contractor screens (`contractor-dashboard.html`) required
   `admin.system` and were admin previews, not customer onboarding.
7. Mobile nav stacked CTA buttons at a fixed `top: 228px`, which overlaps
   when more account links are added.

Existing employee login, lockout, sessions, and CRM authorization were
intact and are preserved.

## C. Files modified / added

Backend: `pipeline/auth/{onboarding,tokens,mailer,rate_limit,rbac,service}.py`,
`pipeline/api/{server,public_preview}.py`, `pipeline/db/{schema.sql,database.py}`,
`pipeline/config/settings.py`, `pipeline/crm/{admin,serializers}.py`.

Frontend: public nav on `home.html`, `for-contractors.html`,
`for-suppliers.html`, `login.html`; new explore/register/welcome/verify/reset
pages; `site.css` / `site.js` mobile menu; admin approve control.

Tests: `pipeline/tests/test_frictionless_onboarding.py`.

## D. New registration workflow

Contractor (required: name, business name, email, password):

1. Create user + `contractor` role, `account_state=ACTIVE`.
2. Open a session immediately.
3. Send a hashed-token verification email.
4. Redirect to the contractor welcome dashboard.

Supplier (business name, contact name, business email, password, phone,
category):

1. Create user + `supplier` role, `PENDING_EMAIL_VERIFICATION`.
2. Open a session and send verification email.
3. After verify → `PENDING_SUPPLIER_APPROVAL`.
4. After admin approve → `ACTIVE`. Never auto-approved.

Duplicate emails return a generic 409 and send a reminder mail. Admin
self-registration is impossible: register endpoints hard-code roles.

## E. Email verification behavior

Required before live RFQs, protected intelligence, private quotations,
transactions, and supplier commercial/contact access.

Not required to sign in, complete onboarding, use the welcome dashboard,
or view public demonstrations.

Copy: “Email verification required to activate this feature.” Resend is
available. Enforcement is in `onboarding.deny_protected_*`.

## F. Role authorization matrix

| Action | Anon | Unverified contractor | Verified contractor | Pending supplier | Approved supplier | Employee | Admin |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Public site / `/api/public/preview` | yes | yes | yes | yes | yes | yes | yes |
| Self-register contractor/supplier | yes | — | — | — | — | — | — |
| Welcome dashboard / profile | no | yes | yes | yes | yes | no | no |
| Live RFQ / private quotes / intel | no | no | yes | no | no | no* | yes* |
| Live supplier RFQs / quotes / contacts | no | no | no | no | yes | no* | yes* |
| Sales / admin CRM | no | no | no | no | no | by role | yes |
| Create admin account | no | no | no | no | no | no | yes |

\* Employee CRM permissions are unchanged and remain separate from the
network contractor/supplier permissions.

## G. Test results

Executed 2026-10-10 on this branch with
`C:\Users\dezna\AppData\Local\Python\bin\python.exe -m pytest`:

| Suite | Result |
| --- | --- |
| `pipeline/tests/test_frictionless_onboarding.py` | **23 passed** (76.95s) |
| `pipeline/tests/test_sales_crm_security.py` | passed in the combined auth run |
| `pipeline/tests/test_workspace_ui.py` | passed in the combined auth run |
| `pipeline/tests/test_option_b_safety.py` | passed in the combined auth run |
| Combined four-file run after the preview-assertion fix | **238 passed**, 4 skipped, 0 failed |

The 4 skips are existing Playwright UI tests whose browsers were not
installed in the first combined run. They were not part of the
onboarding file.

Could not run the full `pipeline/tests` tree in this session (scope was
the auth/onboarding suites above). Device lab testing (physical iPhone
Safari, Android Chrome, Desktop Edge) was not executed; mobile coverage
is CSS/HTML assertions plus Playwright screenshots at 390×844.

## H. Screenshots

Captured against `http://127.0.0.1:18080` with a **temporary empty
database** (`%TEMP%\corridoriq-onboarding-preview.db`). Production was
not opened.

See `docs/operations/onboarding-screenshots/`:

- `home-desktop.png`
- `explore-desktop.png`
- `login-desktop.png`
- `register-contractor-desktop.png`
- `register-supplier-desktop.png`
- `contractor-welcome-desktop.png`
- `reset-password-desktop.png`
- `home-mobile-nav.png`
- `register-contractor-mobile.png`
- `login-mobile.png`

## I. Remaining launch blockers

1. SMTP is optional. Without `CORRIDORIQ_SMTP_*`, mail is written to the
   local outbox (`CORRIDORIQ_MAIL_OUTBOX` / data-dir `mail-outbox`).
2. Live RFQ persistence, real quotations, and paid intelligence are still
   not productized — gates exist, payloads stay empty on purpose.
3. Supplier approval is a manual admin action. No notification workflow
   beyond the verification email.
4. The isolated contractor pilot is a second signup surface (`/join/<code>`).
   Decide whether to retire or deep-link it after launch.
5. Production cutover still needs a reviewed deploy (this branch must not
   be merged or deployed from this work).

## J. Recommended deployment sequence

1. Review this branch; do not merge from the implementing agent.
2. Confirm `CORRIDORIQ_PUBLIC_URL` and SMTP (or an approved mail relay).
3. Apply schema via the existing `init_db` / `migrate_schema` path on a
   staging copy — additive columns only.
4. Run `pipeline/tests/test_frictionless_onboarding.py` plus the existing
   auth/CRM suite against staging.
5. Smoke: anonymous explore → contractor signup → welcome → verify →
   employee login still lands on the sales/admin dashboards.
6. Only then schedule a production deploy through the normal release
   process. Do not change Cloudflare, Railway, billing, or production
   data as part of this branch.
