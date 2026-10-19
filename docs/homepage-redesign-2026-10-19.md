# CorridorIQ Homepage Redesign — October 19, 2026

## Objective
Create a compelling, accessible, mobile-first marketing homepage that explains CorridorIQ's two-sided construction ecosystem in seconds, prioritizes self-service contractor acquisition, and supports high-touch supplier sales.

## Positioning
**Headline:** From finding the job to getting the materials.
**Subhead:** Construction intelligence and simpler procurement for the people who build—and the suppliers who keep them moving.
**Primary pathways:** I'm a Contractor / I'm a Supplier.

## Contractor pathway
- Free: Connect with participating suppliers, build material lists, request quotes or place orders *where supported by current production integrations*.
- Contractor Pro: Future paid tools including AI-assisted photo-to-materials, guided job measurements, per-vehicle inventory, and replenishment.
- Never suggest photo-based measurement, real-time pricing, stock, or purchasing are live unless verified.
- Clear distinction between available now, pilot/limited availability, and planned capabilities.
- Primary CTA: Explore Contractor Tools / Create Free Account only if route and signup are verified.

## Supplier pathway
- Discover construction projects and contractor activity from public permit signals.
- Understand likely materials demand, identify sales opportunities, and connect with buyers.
- Primary CTA: Explore Supplier Intelligence / Request a Demo.

## Visual direction
- Enterprise-grade, editorial SaaS design; dark navy, warm ivory, controlled amber accents.
- No stock construction hero or fabricated dashboard metrics.
- Use a tasteful *illustrative workflow* rather than fake customer data; mark examples clearly.
- Strong hierarchy, readable typography, generous whitespace, keyboard focus, responsive navigation.
- Above fold: headline, value proposition, two audience buttons, clear preview of signal -> material demand -> quote workflow.

## Page architecture
1. Compact header: logo, How It Works, For Contractors, For Suppliers, Product Roadmap, Sign In.
2. Dual-audience hero.
3. How CorridorIQ works: public signals -> actionable opportunities -> material sourcing.
4. Contractor section: free procurement access vs planned paid Contractor Pro; visual of photo -> suggested BOM -> supplier quote, explicitly labeled *concept*.
5. Supplier section: verified permit intelligence, demand discovery, demo CTA.
6. Trust: source transparency, human verification, participating-supplier constraints, geographic coverage only when validated.
7. Closing dual CTA and existing footer/legal links.

## Engineering boundaries
- Base: production release fcf50fe. Isolated design branch; no production deployments or merges.
- Frontend marketing only. No auth, API, DB, email, Cloudflare, Railway, or pricing changes.
- Preserve existing operational routes, form behavior, analytics hooks, and accessibility.
- Keep freight/logistics product off primary marketing page, without deleting backend or legacy assets.
- Do not silently redirect to a route that is not implemented.
- Do not alter shared CSS rules used by dashboard; use homepage-scoped selectors or separate stylesheet.
- Review desktop/mobile render and test interactions before requesting merge.
