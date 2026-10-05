# Product architecture

These concepts are different. Do not collapse them.

## RAW MARKET OPPORTUNITY

`opportunity_score` on a project. It is the rule-based market score from public permit analysis (`ANALYSIS_VERSION` is a rule engine, not an LLM). It is not an account rank and not a sales-lane assignment.

## PROJECT / CUSTOMER RELEVANCE

`customer_relevance_score` (stored as relevance on `project_customer_relevance`). It says how relevant a project is to a customer profile. It is a shadow layer. It does not replace `opportunity_score`.

## ACCOUNT COMMERCIAL PRIORITY

`account_priority_score` on `company_customer_priority`. It ranks accounts for commercial attention. It is not a sales-lane fit and not a contactability grade.

## SALES-LANE FIT

Whether an account belongs in a selling motion. Fit is separate from priority. An account may sit in more than one lane. Presentation changes do not rewrite priority.

Current lane keys:

- PLUMBING_CORE
- FUEL_GAS_PROPANE
- FIRE_BACKFLOW
- CIVIL_WET_UTILITY
- HVAC_MECHANICAL
- GENERAL_CONTRACTOR_CM
- MULTI_TRADE
- SPECIALTY_OTHER
- IDENTITY_REVIEW

`IDENTITY_REVIEW` is for analysts. It is not a default salesperson book.

## CANONICAL ENTITY

The commercial account across raw company aliases. Canonical presentation may collapse duplicate rows in a view. That is not permission to merge company rows or rewrite project foreign keys.

## CONTACTABILITY

Whether a salesperson can legitimately reach the business, and by which public channel. Callability (`CALL_NOW`, `CALL_WITH_CAUTION`, `NO_ACTIONABLE_CONTACT`) is a presentation badge, not a score.

## CRM

Worked commercial relationships. It is not a copy of every discovered account.

## ROC

Arizona ROC license data is an identity-validation layer. Ranking consumption stays off until a later phase explicitly enables it. ROC does not set `opportunity_score`, `customer_relevance_score`, or `account_priority_score`.

## Products

Everything above is shared data infrastructure for supplier intelligence. Contractor procurement keeps its own pilot store under `CORRIDORIQ_PILOT_ROOT` and does not write the intelligence database. See `commercial_models.md`.

## Contractor procurement

Early access for typed or pasted material requests, structuring, and supplier receive/open/acknowledge in the pilot code. Photo-to-BOM, live inventory, account pricing, quoting, payments, and a marketplace are planned. Supplier systems remain authoritative for price, inventory, and availability.

## Fulfillment marketplace

Not authorized. A typed request sent to one chosen supplier is not a marketplace. A readiness label inside an internal audit is not a product.

## Customer dashboard

The live dashboard reads exported JSON. Cutover of a new customer experience is not authorized by this knowledge file. Confirm the latest sales-trial or phase report before claiming a cutover decision.
