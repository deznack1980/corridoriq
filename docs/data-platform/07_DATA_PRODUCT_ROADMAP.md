# 07 — Data Product Roadmap

**Status:** design proposal. No API work is authorised by this document.

The question this answers: what has to be true before CorridorIQ DATA can be sold as a
distinct product from CorridorIQ Intelligence?

---

## 1. The asset, honestly assessed

| Dimension | Today | Needed for a data product |
| --- | --- | --- |
| Geographic coverage | 9 Phoenix-metro jurisdictions (4 more pending) | Metro-complete, then multi-metro |
| Temporal depth | ~2.5 years (125 permits before 2024) | 5+ years, or explicit as-of series from go-live |
| Historical series | **None** — scores dated one day, 47.8% of permits overwritten | Continuous, queryable change history |
| Entity resolution | Name-only, single signal | Multi-signal, tiered, licence-corroborated |
| Relationship confidence | Unpopulated | Every relationship scored and labelled verified/inferred |
| Project entity | Does not exist | Real multi-permit jobs |
| Contact data | Zero | Enriched, consented, PII-governed |
| Provenance | Lost on update | Per-record, per-field traceability |
| Redistribution rights | **Unassessed** | Per-source, documented, enforced |

The honest summary: CorridorIQ has a **good current-state snapshot** of Phoenix-metro
construction permitting and a **defensible entity registry**. It does not yet have a
historical data asset, because history is being overwritten on every run. The permit
corpus and connector discipline are real and valuable; the time-series product does not
exist yet and starts accruing the day RAW capture is switched on.

---

## 2. Product tiers

### Tier 1 — CorridorIQ Intelligence (exists today)

The application. Lead prioritisation, project discovery, contractor intelligence,
opportunity scoring, CRM. Unchanged by this roadmap.

### Tier 2 — Exports and licensed datasets (nearest term)

Scheduled CSV/Parquet drops of GOLD tables under a licence agreement. Requires: GOLD marts,
redistribution rights assessment, PII suppression, coverage metadata, a data dictionary.
Lowest engineering cost, and the fastest way to discover what buyers actually want before
committing to an API contract.

### Tier 3 — Read API

The endpoints below. Requires everything in Tier 2 plus stable public IDs, pagination,
entitlements, rate limiting, and read isolation from the ingestion writer.

### Tier 4 — Feeds, as-of queries, and analytics

Incremental change feeds, point-in-time reconstruction, and derived analytics for real
estate, logistics, finance, insurance and ML use cases. This is where the historical asset
becomes the product, and it is entirely gated on RAW + change events having been running
long enough to have depth.

---

## 3. Target endpoint surface

Design only — not to be built yet.

| Endpoint | Requires | Blocked by today |
| --- | --- | --- |
| `GET /companies` | `curated_company`, `public_id`, cursor pagination | No public IDs, no pagination |
| `GET /companies/{id}` | + `source_record_map` for provenance | Companies are name-only shells (100% missing contact/address) |
| `GET /companies/{id}/projects` | `curated_project_company` with confidence | Relationships are global, not per-project; confidence unpopulated |
| `GET /projects` | `curated_project` | **Project entity does not exist** |
| `GET /projects/{id}` | + `curated_project_permit` | Same |
| `GET /permits` | `curated_permit` | Mixed date formats; no stable public id |
| `GET /markets/{market}/activity` | `gold_market_activity` + coverage metadata | Phoenix (44.9% of corpus) has no valuation — aggregates would mislead without coverage disclosure |
| `GET /companies/{id}/history` | change events | **No history retained** |
| `GET /changes?since=` | trustworthy watermark | `last_updated_at` is corrupted by churn and enrichment bumps |

### Response contract requirements

Every entity response should carry, at minimum:

```json
{
  "id": "opaque-ulid",
  "attributes": { },
  "provenance": {
    "sources": ["phoenix_az"],
    "source_record_ids": ["..."],
    "first_seen": "2026-01-14",
    "last_seen": "2026-09-15",
    "attribution": "City of Phoenix Open Data"
  },
  "confidence": {
    "entity_resolution": "HIGH_CONFIDENCE",
    "relationship": 0.82,
    "verified": true
  },
  "coverage": {
    "valuation_reported": false,
    "contractor_available": true
  }
}
```

The `coverage` block is not optional decoration. With 73.9% of permits carrying null or
zero valuation and 34.2% lacking a contractor link, a response that silently omits this
context is misleading, and a buyer who discovers the gap after purchase is a buyer lost.

---

## 4. Blocking requirements, ranked

| # | Requirement | Phase (per `06`) | Why blocking |
| --- | --- | --- | --- |
| 1 | RAW capture running | 1 | History cannot be backfilled; every week of delay is permanent |
| 2 | Redistribution rights assessed per source | 9 (start now) | Legal exposure; cheap to do early, expensive to discover late |
| 3 | Stable opaque public IDs | 3 | Customers cache IDs permanently; changing them later breaks every integration |
| 4 | Project entity | 5 | "Projects" is the headline noun of the product and it does not exist |
| 5 | PII governance | 6/9 | 15,722 permits carry `owner_name`; 1,776 companies are individuals |
| 6 | Relationship confidence populated | 2/5 | Selling inferred relationships as facts is a reputational and contractual risk |
| 7 | Trustworthy change watermark | 1/4 | No incremental feed is possible without it |
| 8 | Coverage and freshness metadata | 3 | Aggregates are misleading without it |
| 9 | Read isolation from the writer | 0, then Postgres | External reads must not contend with ingestion |
| 10 | Data dictionary and SLA | 8 | Contractual surface |

---

## 5. Governance obligations

### PII

| Exposure | Volume | Treatment |
| --- | --- | --- |
| `permits.owner_name` | 15,722 permits, 10,826 distinct values | Individuals must be classified and suppressed from external products |
| `companies` typed `property_owner` | 1,776 | Flag `is_individual`; exclude from company datasets by default |
| `contacts` | 0 today | Any future enrichment needs a documented lawful basis before ingestion |
| `crm_*` | Customer-owned data | **Never** eligible for any data product; tenancy boundary is absolute |
| `users`, `sessions`, `security_audit_log` | Internal | Never exposed |

Arizona permit data is public record, but "public record" is not the same as "licensed for
commercial redistribution", and the individuals named in it have not consented to appearing
in a commercial dataset. The `is_individual` flag in `05` exists for exactly this.

### Source licensing

No source has had its terms assessed. Before any external distribution, each of the nine
connected jurisdictions needs a recorded decision on licence, attribution text,
redistribution permission and rate-limit obligations. `redistribution_allowed` defaults to
NULL (unassessed) and must be treated as "no" by any export or API.

One specific note carried over from `jurisdictions.yaml`: Scottsdale's connected feed is
the open-data table, **not** Accela Citizen Access, which the codebase flags as a ToS
concern. That distinction must survive into any future connector work.

### Secrets

The scan was clean. `SOCRATA_APP_TOKEN` is read from the environment; the only credential
literals in the repository are demo passwords in `pipeline/tests/*` and
`pipeline/reports/ui_screenshots.py`. The database file is not tracked by git and does not
appear in git history. No action required beyond keeping it that way.

---

## 6. Indicative sequence

Deliberately expressed as ordering and dependency, not dates — these are engineering-time
estimates, not commitments.

| Stage | Work | Unblocks |
| --- | --- | --- |
| **Immediate** | Phase 0 safety + Phase 1 RAW capture | History starts accruing; corruption risk removed |
| **Near** | Phases 2–4: lineage, CURATED shadow, change events | Provenance and history become queryable |
| **Mid** | Phase 5 project entity + Phase 6 ROC enrichment | The product's headline noun exists; resolution gets real signals |
| **Mid-late** | Phase 7 cutover + Phase 8 GOLD | App runs on the new foundation; marts exist |
| **Late** | Tier 2 licensed exports | First external revenue, lowest cost to test demand |
| **Later** | Tier 3 read API, probably on Postgres | Full data product |
| **Ongoing** | Coverage expansion (4 pending AZ cities, then beyond metro Phoenix) | Market credibility |

Temporal depth is the one thing engineering cannot accelerate. A five-year history is
five years away no matter how the platform is built — which is the entire argument for
starting RAW capture now rather than after the architecture is perfect.

---

## 7. What would make this asset genuinely defensible

1. **Continuous history nobody else kept.** Municipal portals show current state; most
   competitors re-scrape current state. A clean, hash-deduplicated, five-year change
   history of every permit in the metro is not reproducible retroactively by anyone.
   This is the moat, and it costs one phase of work to start.
2. **Resolved entities with confidence.** A contractor graph that distinguishes verified
   from inferred relationships is worth more than a larger, murkier one.
3. **True project grouping.** Turning 110k permits into ~50k real jobs is the difference
   between a permit feed and a construction-project dataset.
4. **Honest coverage disclosure.** Publishing what is missing — Phoenix valuation, Buckeye
   and Gilbert contractors, four unconnected cities — builds more trust than silently
   filling the gaps, and the connector code already takes exactly this stance.
