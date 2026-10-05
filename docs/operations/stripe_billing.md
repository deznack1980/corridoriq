# CorridorIQ billing with Stripe (Founding Supply Partner + Contractor Pro)

Status: **built, not activated.** Off by default (`CORRIDORIQ_BILLING_ENABLED`
unset). While off, `/api/billing/*` and `billing.html` answer 404 and nothing
calls Stripe. No live webhook endpoint is registered.

## Stripe objects (CorridorIQ account — live)

| Object | ID |
|---|---|
| Product | CorridorIQ Founding Supply Partner — `prod_VNrzzzFA4mhRce` |
| Price | $750 USD / month recurring — `price_1UN6Fd4RtdKUQkwzqRlc3bPc` |
| Lookup key | `corridoriq_founding_supply_partner_monthly` |
| Statement descriptor | `CORRIDORIQ` |
| Product | CorridorIQ Contractor Pro — `prod_VNuyXNl2EIWREr` |
| Price | $99 USD / month recurring — `price_1UN9904RtdKUQkwzmuCRRHRd` |
| Lookup key | `corridoriq_contractor_pro_monthly` |

These live objects already exist. Do not create another Contractor Pro product
or price. Prices are configuration (`STRIPE_FOUNDING_SUPPLY_PRICE_ID`,
`STRIPE_CONTRACTOR_PRO_PRICE_ID`), and "$750/month" / "$99/month" are display
text only (`pipeline/billing/plans.py`). Nothing here uses any other company's
Stripe account, keys, products or webhooks.

TEST-mode objects used for validation (Corridor IQ account, sandbox):

| Plan | TEST product | TEST price | Lookup key |
|---|---|---|---|
| Founding Supply Partner | `prod_VNu5qTj6A5Hqpm` | `price_1UN8Gu4RtdKUQkwz3KG6scnF` ($750/mo) | `corridoriq_founding_supply_partner_monthly_test` |
| Contractor Pro | `prod_VNvUJwlgCq9Ice` | `price_1UN9dk4RtdKUQkwzphB7Kkk3` ($99/mo) | `corridoriq_contractor_pro_monthly_test` |

## Plans (`pipeline/billing/plans.py`)

| Plan key | Audience (`organizations.account_type`) | Entitlement | Expected amount |
|---|---|---|---|
| `founding_supply_partner` | `supplier` | `supplier_intelligence` | 75000 USD / month |
| `contractor_pro` | `contractor` | `contractor_pro` | 9900 USD / month |

The organization's `account_type` (set by CorridorIQ, default `supplier`)
decides the **only** plan it may buy. The browser never chooses plan, price,
quantity, customer, account type or entitlement. A contractor subscription can
never grant `supplier_intelligence` and a supplier subscription can never grant
`contractor_pro`: entitlement requires plan audience == organization account
type **and** the stored price == that plan's configured price.

Contractor accounts are organizations with `account_type='contractor'` whose
users hold the `contractor_owner` role (`billing.manage` only; landing page
`billing.html`; portal navigation shows only "Contractor Pro"). There is no
self-service sign-up yet: an administrator creates the organization and user.
Do not give contractor organizations supplier roles (sales_manager etc.); RBAC,
not billing, controls access to supplier screens.

Contractor Pro positioning is deliberately modest: "Priority access to
CorridorIQ's contractor workflow and premium capabilities as they become
available." No response-time, inventory, pricing, savings or lead guarantees.
`entitlements.request_priority(conn, org_id)` returns `priority` for an active
Contractor Pro organization (else `standard`). Material-request creation uses
that result for the billing organization that owns the signed-in contractor's
email. It carries no service-level commitment.

## Architecture

```
supplier organization (organizations.id)            CorridorIQ: application access
   │  billing_accounts.stripe_customer_id  (created server-side, 1:1, UNIQUE)
   ▼
Stripe Customer ── Checkout Session (mode=subscription, server-chosen price)
   ▼
Stripe Subscription                                  Stripe: payment state
   ▼  signed webhook  → re-fetch subscription from Stripe API
billing_accounts.billing_state / price / period / livemode
   ▼
pipeline.billing.entitlements.supplier_has_paid_access()  → entitlements
```

- The paying unit is an **organization** (`organizations`): users, sessions,
  RBAC and CRM data already hang off `organization_id`. The file-based tenant
  registry (`pipeline/tenancy`, book matching) is not involved.
- CorridorIQ's own organization (slug `corridoriq`) is never billable.
- Each supplier that pays must therefore have **its own organization** with its
  users in it. A supplier whose users sit in the `corridoriq` organization
  cannot subscribe until it is moved to its own organization.

| Module | Role |
|---|---|
| `pipeline/billing/config.py` | env config, test/live detection, fixed URLs |
| `pipeline/billing/gateway.py` | the only Stripe SDK caller; signature check |
| `pipeline/billing/service.py` | status, Checkout, Customer Portal |
| `pipeline/billing/webhook.py` | verified, idempotent event processing |
| `pipeline/billing/states.py` | internal state + Stripe status mapping |
| `pipeline/billing/entitlements.py` | the only place that grants paid access |
| `pipeline/billing/store.py` | `billing_accounts`, `billing_stripe_events` |
| `billing.html` / `billing.js` | portal page (Subscribe / Manage Billing) |

## Routes

| Route | Auth | Purpose |
|---|---|---|
| `GET /api/billing/status` | session | plan, state, paid access, can_manage |
| `POST /api/billing/checkout` | session + `billing.manage` + JSON | returns Stripe Checkout URL |
| `POST /api/billing/portal` | session + `billing.manage` + JSON | returns Customer Portal URL |
| `POST /api/billing/stripe/webhook` | Stripe-Signature | event intake |

`billing.manage` is granted to the `admin`, `sales_manager` and `contractor_owner` roles.
Request bodies of checkout/portal are discarded: price, quantity, customer,
redirect URLs and entitlement are decided by the server. Success, cancel and
return URLs are built from `CORRIDORIQ_PUBLIC_BASE_URL` (never the Host header):

- success `…/billing.html?checkout=success&session_id={CHECKOUT_SESSION_ID}`
- cancel `…/billing.html?checkout=canceled`
- portal return `…/billing.html`

The server only hands the browser `https://checkout.stripe.com/…` or
`https://billing.stripe.com/…` URLs, and `billing.js` re-checks the prefix.

## Checkout behaviour

1. Permission and billable organization are checked; config must be valid.
2. A Stripe Customer is created once per organization (idempotency key
   `corridoriq-<mode>-org-<id>-customer-<account created_at>`, under a
   process lock), with metadata `corridoriq_tenant_id`,
   `corridoriq_account_type=supplier`, `corridoriq_plan`. Name = organization
   name. No emails or personal data in metadata.
3. Refused (409) if the organization already has a live subscription.
4. An open Checkout Session that is still valid for 10+ minutes is resumed
   instead of creating another (double-click / two tabs).
5. Session: `mode=subscription`, `customer`, `client_reference_id=<org id>`,
   one line item of the configured price, metadata on session and subscription.
6. State becomes `checkout_pending`. **No access is granted here.**

The success page says *"Your subscription is being confirmed"* and polls
`/api/billing/status`; it reports confirmation only when a verified webhook has
set paid access.

## Webhook

Endpoint (future): `https://corridoriq.pro/api/billing/stripe/webhook`

Processing order: config ready → body ≤ 512 KB → Stripe signature (5-minute
replay window, SDK constant-time compare) → envelope shape → `livemode` equals
the key's mode → claim event ID (`billing_stripe_events`; duplicates return
200 without side effects; earlier `error` attempts are retried) → resolve the
organization **only** from the stored Stripe Customer ID → cross-check
`client_reference_id` / metadata (mismatch = rejected) → **re-fetch the
subscription from Stripe** → verify it belongs to that customer and mode →
update `billing_accounts` → audit.

| Event | Use |
|---|---|
| `checkout.session.completed` | link subscription to the organization |
| `checkout.session.expired` | clear pending Checkout |
| `customer.subscription.created` / `.updated` / `.deleted` | state, period, cancellation |
| `customer.subscription.paused` / `.resumed` | state |
| `invoice.paid` | renewal / recovery (resync) |
| `invoice.payment_failed` | failure (resync + audit) |
| `invoice.payment_action_required` | SCA needed (resync) |

Invoice → subscription is read from `invoice.parent.subscription_details.subscription`
(API 2025-03-31 and later) with fallback to `invoice.subscription`.
`current_period_end` is read from subscription items (API 2025-03-31 and later)
with fallback to the subscription. The installed SDK (stripe-python 15.3.0)
pins API version `2026-06-24.dahlia`; create the webhook endpoint with that
API version.

Responses: 200 handled/ignored (Stripe stops), 400 bad signature/payload/mode,
503 config missing or Stripe API unreachable, 500 unexpected — Stripe retries
503/500 with backoff for up to 3 days, and the retry is processed normally.

Ignored without side effects: unknown customers (other integrations on the
account), non-subscription Checkouts, unsupported event types. A second live
subscription for an organization that already has one is **not** applied; it
is audited as `billing_duplicate_subscription` for manual cancel/refund.

## States and entitlement

| Stripe `status` | `billing_state` | Paid access |
|---|---|---|
| — | `none` / `checkout_pending` | no |
| `incomplete` | `incomplete` | no |
| `incomplete_expired` | `incomplete_expired` | no |
| `trialing` | `trialing` | yes* |
| `active` | `active` | yes* |
| `past_due` | `past_due` | **no** (no grace period — business decision) |
| `unpaid` | `unpaid` | no |
| `paused` | `paused` | no |
| `canceled` | `canceled` | no |
| anything else | `unknown` | no |

\* only if also: price = configured price, livemode = configured mode,
organization active, period end known and not more than 3 days in the past,
config valid. Any exception → no access. `cancel_at_period_end` keeps access
until Stripe ends the subscription.

`supplier_has_paid_access()` / `entitlements_for()` grant `supplier_intelligence`.
**They are not yet called by any existing page or API** — current access is
unchanged until gating is separately approved.

## Audit events (`security_audit_log`, resource_type `billing`)

`billing_checkout_started`, `billing_portal_session_created`,
`billing_subscription_activated`, `billing_subscription_updated`,
`billing_payment_failed`, `billing_subscription_canceled`,
`billing_price_mismatch`, `billing_duplicate_subscription`,
`billing_webhook_rejected`. Details hold event type and state only — no Stripe
payloads, customer IDs, card data or secrets.

## Configuration

Names only in `.env.example`. Secrets live in the server process environment,
never in git (`.gitignore` ignores `.env*` except the example).

| Variable | Notes |
|---|---|
| `CORRIDORIQ_BILLING_ENABLED` | `1` to enable |
| `CORRIDORIQ_PUBLIC_BASE_URL` | e.g. `https://corridoriq.pro` (https; http only for localhost) |
| `STRIPE_SECRET_KEY` | test: `sk_test_`/`rk_test_`; live `sk_live_`/`rk_live_` **only** with `CORRIDORIQ_ENV=production` |
| `STRIPE_PUBLISHABLE_KEY` | optional; must match the secret key's mode |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` of this environment's endpoint |
| `STRIPE_FOUNDING_SUPPLY_PRICE_ID` | live: `price_1UN6Fd4RtdKUQkwzqRlc3bPc`; test: a test-mode price |
| `STRIPE_FOUNDING_SUPPLY_PRICE_LOOKUP_KEY` | optional, `corridoriq_founding_supply_partner_monthly` |
| `STRIPE_CONTRACTOR_PRO_PRICE_ID` | optional until Contractor Pro is sold; must differ from the Founding price. Unset = contractor Checkout answers 503, suppliers unaffected |
| `STRIPE_CONTRACTOR_PRO_PRICE_LOOKUP_KEY` | optional cross-check for `verify-price` |

Recommended live key: a **restricted key** (`rk_live_`) with write access to
Customers, Checkout Sessions, Customer Portal sessions and read access to
Subscriptions, Prices, Invoices only.

Checks (no secrets printed):

```
python -m pipeline.billing check-config              # offline
python -m pipeline.billing verify-price              # read-only GET, test keys
python -m pipeline.billing verify-price --allow-live # read-only GET, live keys
```

## Stripe Dashboard setup (manual)

1. **Customer Portal** (Settings → Billing → Customer portal), test and live
   separately: enable payment method update, invoice history, cancel
   subscription (recommend "at end of billing period"); disable plan switching
   and quantity changes (single plan); set business name, support email
   `archie@corridoriq.pro`, terms/privacy links when available; default
   return URL `https://corridoriq.pro/billing.html`.
2. **Webhook endpoint** (later, when authorized): URL above, API version
   `2026-06-24.dahlia`, the events in the table. Copy its signing secret into
   `STRIPE_WEBHOOK_SECRET` for that environment only.
3. Emails: enable successful-payment receipts and failed-payment emails
   (Settings → Customer emails); Smart Retries per the dunning policy.
4. Optional: a restricted key as above.

## Test mode end-to-end (before any live use)

1. In Stripe **test mode** (same account): create a test Product and a $750/month
   test Price (test objects are separate from the live ones; the live price ID
   does not exist in test mode). Configure the test Customer Portal.
2. Create a test webhook endpoint or run
   `stripe listen --forward-to http://127.0.0.1:8780/api/billing/stripe/webhook --events …`
   and use the printed `whsec_` secret.
3. On a **non-production** server with a copy of the database (never the
   production DB): set `CORRIDORIQ_BILLING_ENABLED=1`, `sk_test_` key, test
   price, `CORRIDORIQ_PUBLIC_BASE_URL=http://127.0.0.1:8780`.
4. `python -m pipeline.billing check-config` and `verify-price`.
5. Create a supplier organization and a `sales_manager` user in it; sign in;
   open `/billing.html`; subscribe with card `4242 4242 4242 4242`.
6. Confirm: page shows "being confirmed" then "confirmed"; `billing_accounts`
   row active; audit events present. Repeat with `4000 0000 0000 0341`
   (fails on renewal) / `4000 0025 0000 3155` (3-D Secure), portal cancel,
   `stripe trigger` duplicates, and a stopped server (webhook retries).

## Production activation (separately authorized)

1. Back up the database. Deploy the approved commit; on first start
   `init_db()` creates the two empty billing tables, adds
   `organizations.account_type` (`NOT NULL DEFAULT 'supplier'`, so every
   existing organization stays a supplier), and seeds the `billing.manage`
   permission, its grants and the `contractor_owner` role (additive only).
2. Configure the live Customer Portal; create the live webhook endpoint.
3. Set live env vars on the production service (restricted live key, live
   price, live `whsec_`, `CORRIDORIQ_ENV=production`,
   `CORRIDORIQ_PUBLIC_BASE_URL=https://corridoriq.pro`), then
   `check-config` and `verify-price --allow-live`.
4. Set `CORRIDORIQ_BILLING_ENABLED=1` and restart.
5. Cloudflare: the webhook path must not be challenged, cached or rate-limited
   like the login rule; no Bot Fight / JS challenge on `/api/billing/stripe/webhook`
   (Stripe cannot solve challenges). HTTPS is required by Stripe for live endpoints.
6. Monitor Stripe → Webhooks → endpoint for failed deliveries, plus
   `billing_stripe_events` rows with outcome `error`/`rejected:*`, and
   `billing_duplicate_subscription` / `billing_price_mismatch` audit events.

## Rollback

- Unset `CORRIDORIQ_BILLING_ENABLED` (or set `0`) and restart: routes and page
  return 404 immediately; existing subscriptions keep billing in Stripe.
  Disable the webhook endpoint in Stripe if needed (Stripe queues/retries
  events for 3 days; re-enabling replays them and idempotency absorbs them).
- Code rollback: redeploy the previous commit. The two billing tables and the
  permission rows are additive and harmless to older code; drop them only if
  they are empty.
- Customer-facing changes (refunds, cancellations) are made in the Stripe
  Dashboard, never by code.

## Security assumptions

- Stripe card data never touches CorridorIQ (hosted Checkout / Portal).
- The session cookie is `SameSite=Lax`, `HttpOnly`, `Secure` in production;
  billing POSTs additionally require `Content-Type: application/json`.
- Organization comes from the server session; Stripe Customer IDs are never
  accepted from clients (no IDOR / customer takeover path).
- Secrets are read from the environment, excluded from reprs, logs, audit
  details and API responses; Stripe errors are logged as class + request ID.
- Live keys are refused unless `CORRIDORIQ_ENV=production`; events whose
  `livemode` differs from the key's mode are rejected; entitlement requires
  matching livemode.
- TLS verification of the Stripe SDK is left at its default (on).

## Contractor Pro: webhook and checkout specifics

- Checkout picks the plan from the organization's account type and the price
  from that plan's configuration; a still-open Checkout Session is resumed
  only if its single line item is that price (sessions are retrieved with
  `expand=["line_items"]`).
- Webhook: after re-fetching the subscription, every item must be exactly the
  organization's plan price. Another plan's price -> `applied:plan_mismatch`;
  any other price -> `applied:unknown_price`; both store the price (so state
  is visible to operators), audit `billing_price_mismatch` and grant nothing.
- CorridorIQ metadata on a Checkout Session or on the re-fetched subscription
  (`corridoriq_tenant_id`, `corridoriq_account_type`, `corridoriq_plan`) must
  match the organization when present, else `rejected:*_mismatch`.
- An organization with no eligible plan (internal org, unknown account type)
  -> `rejected:no_eligible_plan`.

## Live Contractor Pro already exists — do not create another

The live product `prod_VNuyXNl2EIWREr` and price
`price_1UN9904RtdKUQkwzmuCRRHRd` ($99/month, lookup
`corridoriq_contractor_pro_monthly`) are already on CorridorIQ's Stripe
account. Do not create a second product or price.

When billing is separately approved:

1. Live Customer Portal: keep **plan switching disabled**; use a plan-neutral
   headline (both plans share the default portal configuration).
2. Set `STRIPE_CONTRACTOR_PRO_PRICE_ID` to `price_1UN9904RtdKUQkwzmuCRRHRd`
   and the lookup key to `corridoriq_contractor_pro_monthly` on the production
   service only. Run `python -m pipeline.billing check-config` and
   `python -m pipeline.billing verify-price --plan contractor_pro --allow-live`
   (read-only; checks 9900 USD / month, active, mode).
3. Create contractor organizations (`account_type='contractor'`) and
   `contractor_owner` users for the first customers.
4. Public purchase copy stays "coming soon" until that verification succeeds.
   `for-contractors.html` already states $99/month and that purchase is not open.

## v0.2 — integration contracts (Contractor Pro hardening)

### Priority Requests: `request_priority()` is the contract

`pipeline.billing.entitlements.request_priority(conn, organization_id)` returns
`"priority"` (`PRIORITY`) only when `contractor_has_pro()` is true — a contractor
organization whose Contractor Pro subscription is verified paid — and
`"standard"` (`STANDARD`) in every other case: no subscription, checkout pending,
incomplete / failed first payment, 3-D Secure pending, **past_due (no grace
period: `PAID_ACCESS_STATES` = active, trialing)**, unpaid, paused, canceled,
supplier organizations, wrong-plan payments, misconfiguration or any error.

Pilot sign-in and Contractor Pro billing remain separate databases. The link
is the authenticated pilot user's email, not a second account and not a client
field. When a material request is created, CorridorIQ looks up that email on
the application database. Exactly one active user in one active
`account_type='contractor'` organization is authoritative. `request_priority`
runs against that organization. No match, or more than one match, stores
`standard`. An existing pilot user with no Contractor Pro organization does
not become Pro. The pilot-platform organization is never the billing
organization.

1. The organization id comes from that email lookup, never from the request
   body, query string, or headers.
2. Call `request_priority(conn, organization_id)` when a request is **created**
   and store the result on the request row (`priority` is `priority` or
   `standard`), so later billing changes do not silently reclassify history.
3. Ignore any client-supplied `priority`, `plan`, `entitlement`, `account_type`
   or price field.
4. Use only "Priority Request" / "priority routing" language: no response-time,
   quote, inventory, pricing or fulfilment guarantee.
5. Read-only display: `GET /api/billing/status` includes `request_priority`
   (contractors; `null` for suppliers) and `priority_requests` (bool).

### Contractor account provisioning

`POST /api/admin/contractor-accounts` (and `contractor-accounts.html`) — CorridorIQ
operators only (`admin.system` **and** a member of CorridorIQ's own organization);
`GET` lists contractor accounts with billing state and request priority. Both
answer 404 unless billing is enabled; POST requires `Content-Type: application/json`.
Creates, in one operation, an `organizations` row (`account_type='contractor'`),
a `contractor_profiles` row and one `contractor_owner` user. The owner gets the
existing admin-created-user credential flow: a random temporary password,
stored only as a hash, returned once to the operator (never logged or emailed),
and a forced password change at first sign-in. Account type, role, plan and
permissions are fixed server-side; any such input is ignored.

Gap (integration requirement): there is **no email verification, invitation
link or self-service signup** in this branch. Do not bolt one onto this
endpoint; build it in the integration branch on the contract below.

### Signup data contract (`validate_contractor_signup`)

| Field | Required | Rules |
|---|---|---|
| `contact_name` | yes | 2–120 chars, no control characters |
| `email` | yes | normalized lower-case; unique across users |
| `company_name` | yes | 2–160 chars |
| `business_zip` | one of ZIP / address | `NNNNN` or `NNNNN-NNNN` |
| `business_address` | one of ZIP / address | 5–200 chars |
| `roc_license` | **no** (purchasers are often not the license holder) | letters/digits/hyphens, upper-cased |
| `phone` | no | 10–15 digits after stripping formatting |

A future public signup endpoint must reuse this validator and additionally
provide: email ownership verification before activation (time-limited
single-use token, stored hashed), rate limiting / bot protection, no account
enumeration in responses, `must_change_password`-style first-login or
passwordless set-up, and an audit event. Nothing else (SSN, EIN, licence
documents, payment details) is collected.

### Billing health contract (`pipeline/billing/health.py`, contract_version 1)

`billing_health(conn)` → JSON-safe dict, local state only (`basis:
"local_application_state"`): `status` ∈ `NOT_ENABLED | HEALTHY | WARNING |
CRITICAL | UNKNOWN`, `enabled`, `mode`, `configuration` (problems as variable
names / rules only, per-plan `price_configured` booleans, `webhook_secret_configured`),
`webhooks` (last received / last applied timestamps, unresolved errors, oldest
unresolved error, stuck-processing and 7-day rejection counts), `incidents`
(30-day counts of wrong-plan payments, duplicate subscriptions, rejected events),
`subscriptions_in_paid_state` (counts by account type) and `reasons`.

| Status | Meaning |
|---|---|
| `NOT_ENABLED` | billing switched off — **not an error** (default outside a billing launch) |
| `HEALTHY` | enabled, configured, no locally observed problem (says nothing about Stripe's own availability) |
| `WARNING` | webhook failures awaiting Stripe retry, stuck or rejected events, or wrong-plan / duplicate subscriptions needing operator review |
| `CRITICAL` | enabled but misconfigured, or a webhook failure older than Stripe's 3-day retry window |
| `UNKNOWN` | billing tables unreadable |

Never included: API keys, webhook secrets, Stripe customer/subscription/price
IDs, payment details. It never calls Stripe. Surfaces:
`GET /api/admin/billing/health` (`pipeline.monitor`; works while billing is off)
and `python -m pipeline.billing health` (read-only DB). A system-health layer
should consume `billing_health()` directly after branch integration.

### Wrong-plan / duplicate payments: operator visibility

`GET /api/admin/billing/incidents` (CorridorIQ operators) and
`python -m pipeline.billing incidents` list, for the last 30 days: incident type
(`wrong_plan_payment`, `duplicate_subscription`, `webhook_rejected`),
`occurred_at`, organization id/name, account type, expected plan, mismatch type
(`plan_mismatch`, `unknown_price`, `customer_mismatch`, …), observed plan, the
Stripe **subscription** ID involved (from v0.2 audit details) and the operator
action. No customer IDs or payment details. CorridorIQ never refunds
automatically: refunds/cancellations are an owner action in the Stripe Dashboard.

### Concurrency and deployment constraint

Database-level guards (work across threads **and** processes on the one SQLite file):

- `billing_accounts.organization_id` PK (one billing row per organization);
  `stripe_customer_id` and `stripe_subscription_id` UNIQUE.
- Event idempotency: `INSERT OR IGNORE` on the `billing_stripe_events.event_id` PK.
- Every webhook write to `billing_accounts` is compare-and-set on the new
  `version` column; a lost race raises `ConcurrentBillingUpdate`, the event is
  marked `error` and answered 503, and Stripe's retry re-fetches fresh state.
- The Stripe Customer is stored with set-if-absent; its creation uses a
  deterministic idempotency key, so concurrent creators get the same Customer.
- Recording a Checkout Session is one atomic statement that cannot overwrite a
  live subscription state.

Still process-local: the "resume the open Checkout Session" check. Two
processes could each create a session for the same organization; if both were
paid, the second subscription is caught as `duplicate_subscription` (never
applied, audited, refund manually). **Deployment constraint until hardened:
run exactly one CorridorIQ server process (one billing webhook writer).** The
prepared production supervisor already runs a single process.
