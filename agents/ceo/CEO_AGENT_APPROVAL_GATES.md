# CEO agent approval gates

Source list: `company/approval_gates.json`. Enforcement: `agents/ceo/approval.py`.

`propose(action)` returns `execute: false` and `performed: false`. `execute()` always raises `PermissionError`. v0.2 does not add an execution path.

## Founder approval required

Production deployment. Production database writes outside the established pipeline. Deleting production data. DNS, Cloudflare, or domain changes. Customer outreach, email, SMS, phone calls, social posts. Pricing commitments, contracts, payments, purchases, subscriptions, paid APIs. Hiring. External data licensing. Merging protected branches. Public claims of a new capability.

## Allowed without approval

Analyze, prioritize, research, draft, propose tasks, generate a Cursor or Claude assignment, produce a brief, identify risks, prepare a recommendation.

Generating a brief that says "call this account" is a recommendation. It is not a call.

## Claims

`may_market_as_live` is true only for maturity `LIVE`. Photo-to-BOM, inventory, and pricing are `PLANNED`. Public launch is false until `company/launch.json` is replaced with cutover evidence. The loader refuses a seed file that marks the company publicly launched or sets an approved public price.
