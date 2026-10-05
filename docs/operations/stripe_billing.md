# Supplier billing with Stripe

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

These belong to CorridorIQ's own Stripe account. Nothing here uses any other
company's Stripe account, keys, products or webhooks. The code never creates
products or prices; the price is configuration (`STRIPE_FOUNDING_SUPPLY_PRICE_ID`),
and "$750/month" is display text only (`pipeline/billing/config.py`).

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

`billing.manage` is granted to the `admin` and `sales_manager` roles.
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
   `init_db()` creates the two empty billing tables and the `billing.manage`
   permission/role grants (additive only).
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
