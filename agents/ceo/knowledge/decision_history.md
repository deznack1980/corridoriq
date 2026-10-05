# Decision history

Durable decisions only. Cohort percentages, Top 25 membership, and source outages belong in generated reports. Re-read them.

## Scores stay in their lanes

`opportunity_score` remains the raw project market score. Customer relevance and account priority were added as shadow layers. They do not overwrite the project score. Demo quality is not a reason to retune weights.

## Sales lanes are presentation

Phase 4F separated selling motions, including PLUMBING_CORE, from a mixed account book. Lane fit does not replace `account_priority_score`. Multi-lane membership is allowed. Canonical duplicate presentation is allowed. Destructive company merges and project foreign-key rewrites were not part of that decision.

## ROC is identity validation

ROC matching and validation inform who a company is. They are not a ranking engine. Ranking consumption is a separate, explicit switch and stays off until a later authorized phase.

## Contactability is internal

Public business channels can be stored and shown internally. Callability is a badge. It is not an account-priority input unless a future phase says so with evidence.

## CRM is worked relationships

The CRM holds relationships people are actually working. It is not a dump of every discovered account. Bulk-creating CRM rows from a ranked list was refused in the Phase 4G closeout.

## Phase 4G learning trial was prepared

An internal learning trial was prepared from the frozen PLUMBING_CORE book. The phase report refused dashboard cutover, bulk CRM creation, and fulfillment implementation. Whether those calls have since happened must be read from outcome records or a newer report, not assumed here.

## Fulfillment and dashboard

Fulfillment software was not authorized. Customer-dashboard cutover was not authorized. Repeat that claim only when the latest phase artifact still supports it. If the artifact is silent, say UNKNOWN.

## Two commercial lanes, one engine

On 2026-09-24 the founder directed that Intelligence and Fulfillment / demand routing are parallel commercialization options on the same intelligence core. Suppliers are not forced into one model. The CEO evaluates the lanes independently as field evidence, economics, and operational cost arrive. The mandate does not require both lanes to survive forever, and it does not authorize building either lane.

## Two connected products (2026-10-04)

Supplier intelligence and contractor procurement are connected and stay distinct. Suppliers pay for intelligence. Contractors drive network demand. Procurement connects them. Typed material requests are early access in the pilot code. Photo-to-BOM, live inventory, account pricing, quoting, payments, and a marketplace stay planned. Internal pricing hypotheses are not an approved price. John's excitement at Sonoran Plumbing Supply on the Friday before 2026-10-04 is a positive customer-development signal, not willingness to pay, a signed customer, or a paid pilot. Public production launch is not confirmed. Landing commit b2a532a is launch-ready website work only. This supersedes the 2026-09-29 claim that Product B is not being built. It does not authorize a marketplace build, a price, or a customer contact.

## Product A is the focus; Product B is separate and future

On 2026-09-29 the founder clarified the commercial model. Product A, subscription intelligence for supply houses, is the current commercial focus. Product B, a contractor demand / fulfillment engine, is a future hypothesis. The two are separate concepts and must not be blended; no marketplace, order flow, or transaction revenue is assumed. The shared data engine stays shared infrastructure. This supersedes the 2026-09-24 framing of two parallel lanes of equal standing. It is a strategy patch and authorizes no build.

## The portal reads the trust layer

On 2026-09-30 the founder directed that sales-portal surfaces stop ranking and explaining accounts with the legacy `company_priority_score` tier, and read the shared trust layer (`pipeline/trust/`) instead. Account relevance comes from the sales-lane assignment; project feeds hide permits whose own text states only non-wet work; a trust-gated "accounts to act on today" list appears on the sales and admin dashboards. This supersedes the rule that the account-priority layer is never consumed by the dashboard. It keeps the protective half: internal scores, lane fit values, match statuses, license wording, source families, and contact values are never returned by the portal API. The fix is architectural, not a company blacklist. `opportunity_score` still ranks the opportunities list.

## Data trust

Commercial data quality is a standing CEO responsibility. Selling intelligence and acting on intelligence have different trust thresholds. Audit findings live in generated CEO reports, not in this file.

Append to `reports/generated/ceo/decision_log.jsonl`. Do not rewrite this file to match a single day's counts. Promote a lesson here only when it changes the business rules above.
