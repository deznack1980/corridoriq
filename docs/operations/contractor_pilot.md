# Contractor pilot — setup and operation

Code: `pipeline/pilot/`, `pipeline/tenancy/contractor.py`, pages `contractor-*.html`, `supplier-inbox.html`.

## Where data lives

Everything is under one explicit pilot root (`CORRIDORIQ_PILOT_ROOT`), never in the
intelligence database. The root may not be, or contain, the production database folder.

| Path | Holds | Visibility |
|---|---|---|
| `platform.db` | pilot accounts (portal auth tables + code), contractor profiles, supplier connections, referral codes, sent-request snapshots, acquisition events, security audit | server only |
| `contractors/contractor_tenants/<id>/contractor.db` | one contractor's material requests (drafts and sent) | that contractor only |
| `suppliers/` | supplier tenant registry (existing supplier tenancy) | — |

A supplier sees a request only after the contractor presses **Send to …** and confirms;
the supplier reads a snapshot of that one request, never the contractor's store.

If `CORRIDORIQ_PILOT_ROOT` is not set, every `/api/pilot/*` route answers 503 and nothing is created.

## One-time setup (owner)

```powershell
$env:CORRIDORIQ_PILOT_ROOT = "C:\CorridorIQData\pilot"
py -m pipeline.pilot init-supplier --tenant-id sonoran --name "Sonoran Plumbing Supply"
py -m pipeline.pilot referral-code --supplier sonoran --prefix sonoran --label "counter QR"
py -m pipeline.pilot add-supplier-user --supplier sonoran --email <inbox user email> --name "<name>"   # prompts for password
py -m pipeline.pilot qr --base-url https://corridoriq.pro --code <code from above> `
   --utm-source sonoran --utm-medium qr --utm-campaign counter_pilot --out C:\CorridorIQData\pilot\qr\sonoran-counter.svg
```

Then restart the portal server **with `CORRIDORIQ_PILOT_ROOT` set** so the pilot routes are enabled.

Links:

* Contractor entry: `https://<host>/join/<code>` (add `?utm_medium=sales_rep` etc. for rep-shared links)
* Supplier inbox: `https://<host>/supplier-inbox.html`

Scan-test the QR with at least one iPhone and one Android phone before printing anything.

## Acquisition report

```powershell
py -m pipeline.pilot funnel
```

Counts per channel (`qr`, `sales_rep`, `linkedin`, `supplier_referral`, `campaign`, `direct`), referral
code and event. No IP addresses, user agents, emails or names are stored with events.

## Not in this pilot

Email verification (state stored as pending; no email is sent), password reset, inviting
contractor members, quoting, pricing, stock or availability, ordering, payments, any
Contractor Pro feature. The Sonoran catalog is not used.
