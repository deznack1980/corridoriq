# Identity Evidence Shadow

Status: shadow experiment (pass 1). Code: `pipeline/identity_shadow/`.
Production impact: **none** — production is opened read-only (SQLite `mode=ro`
+ `PRAGMA query_only`); all output goes to a separate shadow database.

```
python -m pipeline.identity_shadow --shadow-db C:\CorridorIQData\shadow\identity_evidence_shadow.db ^
    --out C:\CorridorIQData\shadow\reports [--as-of YYYY-MM-DD]
```

## Model

```
SOURCE RECORD → SOURCE IDENTITY → IDENTITY EVIDENCE → SIGNATURE (dedupe within source)
  → BLOCKING → EVIDENCE EVALUATION → RESOLUTION DECISION → CANONICAL COMPANY
CANONICAL COMPANY → ACTIVITY (permits) — activity never decides whether a company exists
```

* Source records reference the production row (table, id, version, payload hash);
  raw payloads are not copied.
* One record may assert several identities (e.g. a contractor and an owner).
* Every identity keeps the source's original string; normalized keys are separate.
* Evidence rows carry family, observed/ingested dates, normalizer version,
  source verification status and CURRENT/STALE/SUPERSEDED state.
* Source tables are upserted with deterministic IDs. Identity, activity and
  analysis tables are rebuilt each run from current source rows; identities not
  seen in the latest run are retained but no longer used (tombstone behaviour).

## Sources (pass 1 — data already held only)

Permit party fields per jurisdiction (Phoenix `PROFESS_NAME`; Scottsdale
`ResponsibleParty`/`Builder`/`Owner`; Mesa `contractor_name`+`contractor_address`,
`applicant`; Peoria `Applicant_Contact_Organization`/`Applicant_Contact_Name`/`ContactPH`;
Tempe contractor name/license/phone/address/email; Goodyear general contractor and
owner with phones and addresses), historical raw versions, the ROC roster,
CorridorIQ's ROC crosswalk (hint only, never independent), and existing contact
channels. Gilbert, Buckeye and Chandler publish no party fields — activity only.
ACC is not used (reserved, disabled; research in progress).

## Policies

**Persons.** A person-like name is not invalid, but it is not a company on its
own. Person names become part of a canonical company only with license evidence,
or with phone/address evidence that links them to a business. Otherwise they
are *held* (no company), and if the name uniquely equals a sole-proprietor ROC
licensee a review case is raised. Contact persons (e.g. Peoria applicant
contacts) are never companies.

**DBA / AKA.** Explicit markers (DBA, D/B/A, AKA, A/K/A, doing business as) split a
string into a legal name and a trade name recorded as a `DBA_OF` relationship
inside one company — not two companies.

**Names.** Suffixes, punctuation, &/AND, THE, spacing and initials are
normalized; trade words are kept ("ABC PLUMBING" ≠ "ABC PLUMBING AND MECHANICAL").

**Shared values.** A phone, street address or domain asserted by more than 3
distinct business names (e.g. a city's own phone on every permit, a registered-agent
address) is non-identifying: kept as evidence, never used to link or counted as
new evidence.

**Locations.** An identity is *located* only with a ZIP or city. A street alone can
corroborate a same-name match but cannot make two same-name observations "different
businesses".

## Resolution (deterministic; no ML, no LLM)

| Rule | Evidence |
|---|---|
| LICENSE_NAME | ROC license + name exact or related (strong) |
| ROC_SAME_NAME_ADDRESS | several license classes, one holder name and address (strong) |
| PHONE_NAME / ADDRESS_NAME / DOMAIN_NAME | non-shared value + exact name (strong) |
| PROD_REFERENCE | contact channel researched for a production company + same-name permit identity (strong) |
| NAME_ONLY_GROUP / NAME_ATTACH / CROSSWALK_ATTACH | name only — CANDIDATE, never confident |

Guards: capped blocks (no all-pairs comparison); unions refused when they would
chain too many names or join different ROC licenses without a shared address;
a license cited under a different name is a CONFLICT; a name matching several
located businesses is REVIEW_REQUIRED.

States: VERIFIED (ROC + another independent family by strong evidence) ·
HIGH_CONFIDENCE (2+ independent families by strong evidence) · CANDIDATE
(multiple families by name only) · SOURCE_ONLY · REVIEW_REQUIRED · CONFLICT.
Repeated evidence from one family counts once.

ROC-only entities are licensed identities with **no observed activity in current
CorridorIQ permit coverage** — not evidence of real-world inactivity.

## Outputs

Shadow tables plus an INTERNAL / CONFIDENTIAL report folder: `summary.md`,
`review_queue.csv`, `would_split.csv`, `would_merge.csv`,
`unrepresented_production.csv`, `source_contribution.csv`, `metrics.json`. CSV cells
are formula-injection safe. No supplier tenant data is read.
