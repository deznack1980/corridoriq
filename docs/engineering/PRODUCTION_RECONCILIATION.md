# Production Reconciliation: Contractor/Supplier Onboarding

| | |
|---|---|
| Branch | `integration/onboarding-production` |
| Base | **`fcf50fe`**, the verified production commit (method below) |
| Onboarding source | `feature/frictionless-onboarding` @ `f152f49` (5 commits on top of `2945e20`) |
| Date | 2026-10-11 |
| Rule | Production security logic wins. Onboarding features are rebuilt on production's modules, feature by feature. Nothing is merged blindly. |

## 1. Verified production baseline

Railway serves **`fcf50fe`** (`release/corridoriq-2026-10-06`):

- Ten served files hash-match `fcf50fe`: `login.html`, `portal.css`, `map-workspace.js`, `landing.html`, `admin-dashboard.html`, `admin-dashboard.js`, `for-contractors.html`, `site.css`, `pilot-common.js` and `experience-preview.html`.
- `experience-preview.html` exists only in `fcf50fe`.
- `robots.txt` and `register-contractor.html` return 404, so no onboarding code is live.
- `fcf50fe` is still the head of `origin/release/corridoriq-2026-10-06`.

## 2. Preserved work (nothing deleted, reset or overwritten)

| Location | Branch / HEAD | State | Handling |
|---|---|---|---|
| Main worktree (Cursor) | `feature/frictionless-onboarding` `f152f49` (pushed) | 44 uncommitted entries: CEO-agent work, screenshots, a pilot test addition, `pipeline/pilot/demo_server.py` | Untouched. Uncommitted work is **not** integrated. Cursor must commit it separately. |
| `CorridorIQ-compliance` | `compliance/soc2-automation` `fcf50fe` | clean | Untouched |
| `CorridorIQ-launch-integration` | `integration/launch-readiness-v01` `1ddbda8` (**local only, never pushed**) | 6 uncommitted entries, including an untracked `requirements.txt` | Untouched. **Risk: this branch exists on one disk only.** See decision D-3. |
| Others | `feature/stripe-billing`, `feature/landing-page`, `feature/ceo-native-data-intelligence-v01`, `C:/CorridorIQ-prod` (detached `d16dcd2`) | clean | Untouched |
| **New** | `integration/onboarding-production` from `fcf50fe` (separate worktree) | — | All work here |

## 3. Commit-by-commit comparison

### 3a. Production commits missing from the onboarding branch (23): all KEEP PRODUCTION

The integration branch starts **from** production, so all 23 are retained by construction.

| Commit | Summary | Area | Class |
|---|---|---|---|
| `b2a532a` | Landing page as public homepage | Website | KEEP PRODUCTION |
| `34f6e36` | Stripe subscription integration (inactive) | Billing | KEEP PRODUCTION |
| `1f936ca` | Owner morning brief | CEO agent | KEEP PRODUCTION |
| `8fa957d` | Contractor Pro on Stripe | Billing | KEEP PRODUCTION |
| `8d39347` | Morning system-health verification | CEO agent | KEEP PRODUCTION |
| `4a233fd` | Harden Contractor Pro | Billing | KEEP PRODUCTION |
| `3a2707c` | Merge CEO system health | CEO agent | KEEP PRODUCTION |
| `0529e2d` | Link Contractor Pro identity to material requests | Pilot | KEEP PRODUCTION |
| `b038ddb` | Stop false schedule claims | Ops | KEEP PRODUCTION |
| `5408bdd` | Stop calling stored permits live | Copy | KEEP PRODUCTION |
| `70aa4c2` | Pilot, priority and **client-IP** launch fixes (`client_address.py`) | **Security** | KEEP PRODUCTION (extended, §5) |
| `5df0d4e` | **Verified contractor identity and recovery**: hashed, purpose-bound tokens | **Auth** | KEEP PRODUCTION |
| `e314fb4` | **Phase-2 security review remediation** | **Auth** | KEEP PRODUCTION |
| `d21c56a` | **Phase-2 security retest fixes** | **Auth** | KEEP PRODUCTION |
| `3f9a66f` | **Postmark transactional delivery** | **Mail** | KEEP PRODUCTION |
| `d16dcd2` | Unified branding and accessibility | UI | KEEP PRODUCTION (approved palette applied on top, §6) |
| `d2ba11a` | Railway runtime compatibility (`PORT`, host) | **Deploy** | KEEP PRODUCTION |
| `f9b2d70` | Root dependency manifest for Railway | **Deploy** | KEEP PRODUCTION |
| `1380d94` | Inline Python dependencies for the Railway build | **Deploy** | KEEP PRODUCTION |
| `f3e358a` | Launch UX and admin experience preview | UI | KEEP PRODUCTION |
| `25cdf21`, `4131b72` | Admin opportunity map stability | UI | KEEP PRODUCTION |
| `fcf50fe` | Merge PR #22 | — | KEEP PRODUCTION |

### 3b. Onboarding commits (5)

| Commit | Summary | Treatment |
|---|---|---|
| `8d335b1` | Public onboarding: registration, verification, welcome pages, preview, RBAC roles, `onboarding.py`, `mailer.py`, `rate_limit.py`, `tokens.py`, schema | **Feature logic ported** onto production modules. Its `tokens.py`, `mailer.py`, `rate_limit.py`, `auth_tokens` schema and login changes are **rejected** (§4). |
| `f5f49f4` | Validation, mail safety, `robots.txt`/`sitemap.xml`, `seo.py`, launch tests | SEO and pages **adopted**. Mail changes **rejected** (production mail kept). Tests **ported**. |
| `53d92f5` | Approved amber-gold/charcoal brand | **Merged carefully:** palette applied to production CSS (§6) |
| `066fabf` | `client_ip.py`, identity-keyed limits, suspended-session check, tolerant migrations | Suspended-session check and tolerant migration **adopted**. `client_ip.py` **rejected** (§5). Identity keying **adopted** inside production's throttle. |
| `f152f49` | Runbooks, proxy and SMTP audit, rollback test | Docs superseded by this file and `INTEGRATED_RELEASE_READINESS.md`. The rollback test is replaced by a real cross-version harness (§7). |

## 4. Area-by-area decisions

| Area | Production (`fcf50fe`) | Onboarding branch | Decision |
|---|---|---|---|
| **Login** | `service.login`: generic errors, dummy-hash timing, lockout; throttle on failures only; JSON-only | Adds an IP-first limiter on *all* attempts | **KEEP PRODUCTION** |
| **Sessions** | Server-side, 256-bit, HttpOnly / SameSite=Lax / Secure (prod), 12 h, revoked on password change and disable | Unchanged, plus `get_current_user` rejects `SUSPENDED` accounts | **KEEP PRODUCTION** + **ADOPT** the suspended check |
| Session hashing at rest | Not in production (exists only on local `integration/launch-readiness-v01` @ `1ddbda8`) | — | **REQUIRES OWNER DECISION** (D-3). Not included in this release. |
| **Password hashing** | Argon2id, scrypt fallback, rehash-on-login | Same module; a separate fixed scrypt dummy for timing | **KEEP PRODUCTION** (`dummy_password_hash()` uses the live algorithm). **REJECT** the scrypt dummy. |
| **Email tokens** | `auth_tokens(consumed_at, revoked_at, email, password_marker, context_json, created_ip)`. Atomic conditional consume; re-issue revokes older tokens; reset bound to password state | `auth_tokens(used_at)`. Read-then-update consume; no revocation | **KEEP PRODUCTION**. **REJECT** the onboarding `tokens.py` and table definition. Verification uses a new production purpose, `portal_email_verify`. |
| **Mail** | Postmark HTTPS API (verified TLS); in-memory sink only outside production; production refuses to start without Postmark; links carry tokens in the URL fragment | SMTP (`starttls()` without a verifying context) + silent in-memory outbox; disk outbox; `?token=` links | **KEEP PRODUCTION**. **REJECT** `mailer.py`. **ADOPT** a *strict* SMTP adapter as an optional provider (verified TLS, no plaintext, no fallback). Add verification and "already registered" templates. |
| **Registration** | Pilot contractor signup (email-first, platform DB); supplier tenants created by operators | `register_contractor` / `register_supplier` in the main DB, default organisation, immediate session | **MERGE CAREFULLY**: rebuilt on production `create_user`, tokens, mail and throttle |
| Contractor identity | Pilot contractor accounts (platform DB, per-tenant stores, live RFQ) | Second contractor account type in the main DB (welcome/demo, live RFQ locked) | **REQUIRES OWNER DECISION** (D-1). Both kept; they are separate stores and separate logins. |
| **Verification gate** | `email_is_verified()`: verified timestamp **and** address equal to the current email | `email_verified_at` only | **KEEP PRODUCTION** semantics. Onboarding gates use the address-bound check. |
| **Supplier approval** | — | Admin (`users.update`) approves verified suppliers; sets active from any state | **ADOPT with fixes**: only `PENDING_SUPPLIER_APPROVAL → ACTIVE`; suspended accounts are not re-enabled; before/after state audited |
| **RBAC** | 7 roles, 37 permissions, per-request reload | `contractor` and `supplier` roles with namespaced permissions; landing pages | **ADOPT** |
| **Employee workspace isolation** | Permission checks per route | `require_employee_workspace()` before every employee route | **ADOPT** (defence in depth) |
| **Rate limiting** | `throttle.py`: per-IP and per-email buckets, failures-only for login | `rate_limit.py`: IP-first on all attempts, plus identity | **KEEP PRODUCTION** `throttle.py`, **extended** with registration, verification and resend buckets keyed by email (§5) |
| **Client IP** | `client_address.py`: `CF-Connecting-IP` only with the proxy secret; otherwise the TCP peer | `client_ip.py`: when the peer is "trusted", accepts `CF-Connecting-IP` **without a secret**, then `X-Forwarded-For`, then `X-Real-IP` | **KEEP PRODUCTION**. **REJECT** `client_ip.py`: with private-range trust enabled on Railway, any visitor could forge `CF-Connecting-IP`. **MERGE CAREFULLY** a narrow Railway mode (§5). |
| **Schema** | `users.email_verified_at`, `email_verified_address` exist | Adds `account_kind`, `account_state`, `business_name`, `business_category` (+ its own `auth_tokens`) | **ADOPT** the four user columns via production's `migrate_schema` list. **REJECT** the `auth_tokens` definition. **ADOPT** the duplicate-column tolerance. |
| **Health endpoint** | Readiness and problem codes; no secrets | Public mail-delivery mode in `/api/health` | **KEEP PRODUCTION**. Mail diagnostics only on an `admin.system` route. |
| **Security middleware** | JSON-only auth writes; static allowlist; nosniff / X-Frame-Options / Referrer-Policy | New public write routes without a JSON check | **KEEP PRODUCTION**, applied to every new public write |
| **Deployment config** | `PORT`/`CORRIDORIQ_HOST`, root `requirements.txt`, `CORRIDORIQ_PUBLIC_BASE_URL`, start gate | `SMTP_*`, `PUBLIC_APP_URL`, `MAIL_OUTBOX_DIR`, `TRUST_PROXY*` | **KEEP PRODUCTION**. **REJECT** the onboarding-only variables. New optional variables are documented in the readiness doc. |
| Public preview, `robots.txt`, `sitemap.xml` | — | Static synthetic data; static SEO lists | **ADOPT** |
| Front-end onboarding pages | — | Register, verify, reset, welcome, explore, `auth-public.*` | **ADOPT with changes**: read tokens from the URL fragment; call production endpoints (aliases kept) |
| Branding | `d16dcd2` palette | Approved amber-gold/charcoal tokens | **MERGE CAREFULLY**: approved values applied to production CSS (§6) |
| Tests | Production suite | Onboarding, launch, release-security, brand and rollback tests | **ADOPT** after porting to production infrastructure; **plus** a mandatory cross-version harness |
| Onboarding-branch docs | — | Runbooks describing the SMTP/outbox and `client_ip` designs | **REJECT** (they describe rejected designs). `docs/design/brand_palette.md` **ADOPTED**. |
| `scripts/restore_backup_isolated.py` | `pipeline.db.backup` (main DB only) | Single-file restore helper | **REJECT** in favour of `scripts/ops/backup_set.py`, which covers every SQLite store |

## 5. Client address and rate limiting (the conflicting design)

- **Railway's documented behaviour:** the edge sets `X-Real-IP` to the client's address ([Railway Specs & Limits](https://docs.railway.com/networking/public-networking/specs-and-limits)).
- **Railway staff (2026-09-29):** `X-Real-IP`, `X-Forwarded-For`, `X-Forwarded-Proto` and `X-Forwarded-Host` are *overwritten* at the edge. Railway publishes **no stable proxy CIDR range**: "100.64.0.0/10 is not a stable range" ([Railway Station](https://station.railway.com/questions/which-client-address-headers-does-the-ra-fb9e36d7)).

Integrated design. Production's `client_address.py` is kept; one opt-in mode and one safety net are added:

1. **Unchanged:** `CF-Connecting-IP` is honoured only with the shared proxy secret. `X-Forwarded-For` is never parsed.
2. **New, opt-in:** `CORRIDORIQ_CLIENT_IP_SOURCE=railway`. Use `X-Real-IP`, a single valid address, **only when the TCP peer is a non-public address**, meaning the connection arrived through Railway's internal network. No CIDR list is hard-coded. A public peer, which on Railway would mean bypassing the edge, falls back to the peer.
3. **New, always on:** when the resolved address is a platform hop (private-network address space) rather than a visitor, per-IP buckets use **shared-address limits** sized for the whole platform. Email- and account-keyed buckets keep the per-visitor anti-abuse role.
4. Registration, verification resend and reset requests are throttled per email **and** per address. Verification and reset completion are throttled per address.

## 6. Brand

Approved values are applied as CSS custom properties in production's stylesheets:
- Gold `#c9a44c`
- Metallic gold `#b8912f`
- Gold on dark `#e2c179`
- Ink `#0e1116`
- Charcoal `#0a0e1a`

A regression test asserts them.

## 7. Required evidence (see `INTEGRATED_RELEASE_READINESS.md`)

- **Cross-version:** production code (`fcf50fe`) creates a database with synthetic employees, sessions, tokens and pilot data. The integrated code must then serve that database: existing logins, sessions and tokens work, and registration, verification, approval and isolation pass.
- **Rollback:** after the integrated code has migrated and written new accounts, production code (`fcf50fe`) must still start and serve existing users from the same database.

## 8. Owner decisions

| ID | Decision | Recommendation |
|---|---|---|
| D-1 | Two contractor account types (pilot platform vs main-DB onboarding) | Ship both for now. They are isolated stores with separate logins. Plan to link a verified main-DB contractor to a pilot tenant before enabling live RFQs for onboarding accounts. |
| D-2 | Mail provider | **Postmark HTTPS API** (production default). Strict SMTP to Postmark is available as a fallback only. |
| D-3 | Session hashing (`1ddbda8`) and the uncommitted work on the local-only `integration/launch-readiness-v01` | Push that branch to preserve it. Release it separately after this one. Hashing invalidates existing sessions once (users sign in again). |
| D-4 | Enable `CORRIDORIQ_CLIENT_IP_SOURCE=railway` | Yes. Verify on a non-production Railway service first (procedure in the readiness doc). |
