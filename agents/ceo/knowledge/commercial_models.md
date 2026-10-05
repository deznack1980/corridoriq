# Commercial models

Founder mandate, 2026-10-04. This supersedes the 2026-09-29 statement that Product B is not being built.

CorridorIQ has two connected products. They stay distinct. Procurement connects them. Do not blend them into a marketplace, and do not assume transaction fees.

- Product A, supplier intelligence, is the closest product to revenue.
- Product B, contractor procurement, is the network-growth product. Typed or pasted material requests are early access where the pilot code implements them. Inventory, account pricing, automated quoting, payments, and a multi-supplier marketplace are planned, not live.

This file does not authorize a deployment, a price, or a customer contact.

## Product A — supplier intelligence

Customer: plumbing supply houses, independent distributors, regional distributors, and later other construction-material suppliers.

What the customer buys: where construction demand is forming, which contractors are involved, which existing accounts are active around that demand, which accounts may be dormant, and which potential customers deserve sales attention.

Pipeline, internal only: source data, normalization, entity resolution, project and permit intelligence, contractor and company, trade, customer relevance, account priority, sales-lane fit, contactability, why now, likely material demand, supplier sales intelligence.

External language stays approximate: "multiple public, commercial and proprietary data signals." Do not publish scoring formulas or source mechanics.

Who owns the contractor relationship for this product: the supplier's sales organization.

Economics: suppliers pay for intelligence. Internal hypotheses live in `company/pricing.json`. No public price is approved.

Trust bar: a salesperson can qualify before acting when uncertainty is visible and inference is labeled.

## Product B — contractor procurement

Initial workflow: contractor, CorridorIQ, material request or BOM, participating supplier receives the request.

Current early access, evidenced in `pipeline/pilot/materials.py` and `docs/operations/contractor_pilot.md`: a contractor can type or paste lines, the system can structure the request, and a participating supplier can receive, open, and acknowledge it when the pilot is actually running.

Not live unless a later evidence record says so: photo-to-BOM, PDF ingestion, spreadsheet ingestion, live inventory, account-specific pricing, automated quoting, quote comparison, payments, checkout, a multi-supplier marketplace, automated compatibility guarantees.

Contractor basic access stays low-friction or free during network formation. Contractor Pro is a hypothesis.

Trust bar: higher than Product A wherever CorridorIQ acts in the workflow. Supplier systems stay authoritative for price, inventory, and availability. AI must not fabricate those.

## Network

Supplier intelligence recruits supply houses. Participation and invitations bring contractors. Material demand flows to participating suppliers. Those procurement signals make the intelligence more valuable.

Lane A, existing customer revenue: make the supplier easier to buy from.
Lane B, new demand: help the supplier find business it does not already have.

Positioning: protect and streamline the business you already have while helping you find business you don't have yet.

## Keeping them distinct

- Do not justify a marketplace build as the next step of Product A.
- Do not describe early-access requests as live inventory or live pricing.
- Do not treat a positive sales conversation as contracted revenue.
- A question that asks to build the marketplace is still a marketplace question, and it is not authorized by the existence of the typed-request pilot.

FACT product_a: supplier intelligence for supply houses; closest current path to revenue; not validated; no approved public price
FACT product_b: contractor procurement; EARLY_ACCESS typed material request in pilot code; marketplace inventory pricing quoting and payments are PLANNED
FACT products_are_separate: true
FACT products_are_connected: true
FACT blend_products: false
FACT assume_marketplace: false
FACT product_b_marketplace_authorized: false
FACT transaction_fee_default: false
FACT shared_data_engine: true
FACT pricing_is_internal_hypothesis: true
