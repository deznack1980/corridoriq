# CEO agent state schema

State is JSON under `agents/ceo/company/`. `load_company_state()` reads it. Runtime snapshots and the decision log stay under `CEO_OUTPUT_DIR` and are not the strategy.

Every file carries `schema`, `updated_at`, `source`, and `reason`. Confidence is present where the record is a judgment.

## Files

| File | Schema | Contents |
| --- | --- | --- |
| `constitution.json` | `corridoriq.ceo.constitution.v1` | Company, market, users, thesis, flywheel |
| `maturity.json` | `corridoriq.ceo.maturity.v1` | `LIVE`, `EARLY_ACCESS`, `PLANNED`, `HYPOTHESIS`, `DEPRECATED` |
| `stakeholders.json` | `corridoriq.ceo.stakeholders.v1` | Organization, person, interactions |
| `kpis.json` | `corridoriq.ceo.kpis.v1` | Metric names. Missing values load as `UNKNOWN` |
| `pricing.json` | `corridoriq.ceo.pricing.v1` | Hypotheses. `approved_public_price` is null |
| `launch.json` | `corridoriq.ceo.launch.v1` | `publicly_launched` false until cutover evidence |
| `priorities.json` | `corridoriq.ceo.priorities.v1` | Operating order and current blockers |
| `approval_gates.json` | `corridoriq.ceo.approval_gates.v1` | Gated and ungated action names |

## Stakeholder interaction

Fields: organization, stakeholder, role, segment, interaction date, interaction type, interests, pain points, objections, requested capabilities, commitments, follow-up date, next action, pricing discussed, willingness to pay, pilot status, conversion status, confidence, evidence source.

A positive signal must list what it must not be classified as. John's record forbids validated willingness to pay, signed customer, contracted revenue, active paid pilot, and confirmed product-market fit.

## KPIs

Groups: supplier commercial, contractor network, intelligence, operations, customer development. A stored value is accepted only as `{status: KNOWN, value, source}`. A bare number raises. The seed file stores no values, so every KPI loads as `UNKNOWN`.

## Claim classes

`FACT`, `INFERENCE`, `HYPOTHESIS`, `RECOMMENDATION`. The Sonoran helper returns all four and does not promote inference into fact.

## Jarvis

`agents.ceo.jarvis.answer(intent)` returns `schema: corridoriq.ceo.jarvis.v1` and `execute: false`. Intents: `morning_brief`, `needs_from_archie`, `blocking_revenue`, `what_changed`, `next_action`.
