# Tenancy — supplier-owned private data

Status: pilot foundation (supplier tenants only). Code: `pipeline/tenancy/`.

## The boundary

CorridorIQ has two kinds of data, and they never share a database:

| | Shared intelligence | Tenant-private data |
|---|---|---|
| Examples | permits, projects, companies, ROC identity, trade lanes, verified contacts | a supplier's customer book, purchase dates, branches, reps, match decisions |
| Owner | CorridorIQ (built from public records) | the supplier that provided it |
| Store | shared database (`settings.DB_PATH`) | that supplier's own `tenant.db` |
| Access from tenant code | **read-only** (SQLite `mode=ro` + `query_only`) | read/write, only through that tenant's `TenantStore` |

**Supplier A's customer book is never available to Supplier B.** Nothing in this
layer pools, aggregates, or compares tenants' private data. Pooled or
cross-tenant use would require explicit future consent and legal terms, and a
separate design.

## Concepts

- **Tenant** — stable ID, display name, type, active flag, created timestamp.
- **TenantType** — `SUPPLIER` only. Contractor tenants are deliberately not implemented.
- **TenantRegistry(root)** — the list of tenants, in `<root>/registry.db`. It holds
  IDs, names, type and status only — never customer data. It is the *only*
  issuer of tenant contexts.
- **TenantContext** — proof that a caller resolved an *active, registered* tenant.
  It cannot be constructed directly; there is **no default tenant**, and every
  tenant-store API fails closed (`TenantError`) without a valid context.
- **TenantStore** — bound to exactly one context at construction. It exposes
  `connect()`, `transaction()` and `ensure_schema()` and nothing that accepts
  another tenant ID or a path.

## Storage layout

```
<tenant root>/
    registry.db                    tenant registry (no customer data)
    tenants/<tenant_id>/tenant.db  one supplier's private data
```

Pilot location: `C:\CorridorIQData\tenants\` (`CORRIDORIQ_TENANT_ROOT` overrides;
the library itself has no default root — callers pass one explicitly).
Automated tests always use temporary directories.

## Defenses

1. **Tenant IDs are the only path input.** Lowercase letters, digits and single
   hyphens, 3–48 characters, no leading/trailing hyphen, no Windows device names
   (`con`, `nul`, `com1`…), no reserved words. IDs are rejected, never "cleaned".
   Display names are never used in paths.
2. **Resolved-path check.** `tenant_dir()` resolves the path (following symlinks
   and junctions) and requires it to sit directly inside `<root>/tenants`.
   `..`, absolute paths, separators, NUL bytes, symlinks and junctions all fail.
3. **Owner stamp.** Every `tenant.db` has a `tenant_meta` row naming its owner.
   Opening a file stamped for another tenant (copied or swapped) fails closed.
4. **Row stamp.** Tenant-owned tables also carry `tenant_id`; queries filter on it.
5. **Status re-check.** Each open re-confirms the tenant is still registered and
   active; deactivation takes effect immediately, even for bound stores.
6. **All-or-nothing writes.** `transaction()` uses `BEGIN IMMEDIATE` (serializes
   concurrent writers) and rolls back on any error.
7. **No shared caches of tenant data.** The only in-memory structure reused
   across tenants is the read-only intelligence index, which contains shared
   data only (tested).

## Future: contractor tenants and sponsored seats

Contractor tenants will be a separate `TenantType` with their own stores.
**A supplier sponsoring a contractor's seats will NOT gain access to that
contractor's private information** (jobs, BOMs, photos, customers). Any sharing
between a contractor and a supplier will be an explicit, per-relationship or
per-action grant made by the data owner.

## Backups and deletion

- Tenant stores are separate files; back them up with the same discipline as the
  shared database (they are confidential customer data). Backups must keep the
  per-tenant separation and be encrypted at rest when stored off-machine.
- Deleting a tenant's data = delete `<root>/tenants/<tenant_id>/` and the matching
  backups, then deactivate the registry row. No shared table contains tenant data,
  so nothing else needs cleaning.

## Known limitations

- Isolation is enforced by the data-access layer and file layout of this
  process, not by OS accounts: anyone with filesystem access to the tenant root
  can read every tenant file. Restrict the directory to the service account.
- No encryption at rest beyond the disk's own.
- No authentication/API surface exists for tenants yet; this is offline tooling.
