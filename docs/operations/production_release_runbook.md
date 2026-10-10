# Production release runbook

Branch: `feature/frictionless-onboarding`.  
This is an application rollback plan. It does **not** downgrade SQLite.

New contractor and supplier rows use additive columns. Reverting the
application leaves those rows in place. Do not `DROP COLUMN` and do not
restore an older database over a newer one unless data loss is accepted.

---

## 1. Pre-deployment backup

On the host that owns the operational database:

```
python -m pipeline.db.backup
python -m pipeline.db.backup --verify-latest
```

Confirm a new `corridoriq_<UTC>.db` file and `OK` integrity.  
Optional: `python -m pipeline.db.replication` if off-site TLS is healthy.

Do not skip this step if anyone has used the live site since the last
verified backup.

## 2. Deployment artifact

Deploy git commit **on this branch only after owner approval**.  
Record the exact hash in the change ticket after push. See
`docs/operations/FINAL_LAUNCH_READINESS.md`.

Do not merge to `main` from this runbook.

## 3. Environment validation (presence, not values)

Required on the Railway / host service (secrets stay in the host store):

| Variable | Required value / note |
| --- | --- |
| `CORRIDORIQ_ENV` | `production` (Secure cookies) |
| `CORRIDORIQ_PUBLIC_URL` | `https://corridoriq.pro` |
| `CORRIDORIQ_SMTP_HOST` | Approved relay — empty means outbox only |
| `CORRIDORIQ_SMTP_PORT` | `587` or `465` |
| `CORRIDORIQ_SMTP_USER` | Set |
| `CORRIDORIQ_SMTP_PASSWORD` | Set, never logged |
| `CORRIDORIQ_SMTP_FROM` | `CorridorIQ <noreply@corridoriq.pro>` once SPF allows it |
| `CORRIDORIQ_TRUST_PROXY` | Leave unset / `0` until origin isolation is proven |
| `CORRIDORIQ_TRUSTED_PROXIES` | Explicit `/32` hop only; do not use `private` |
| `CORRIDORIQ_DB_PATH` or `CORRIDORIQ_DATA_DIR` | Existing persistent volume |

Check: `python -m pipeline.auth.mailer diagnose`  
Must not print a password. `smtp_configured` should be true before
customer verification mail is promised.

## 4. Database migration

Restart the portal process so `init_db()` / `migrate_schema()` run.

Expected: additive columns on `users` and `CREATE TABLE IF NOT EXISTS`
for `auth_tokens`. Existing emails and password hashes stay.

If migrate raises, **stop**. Do not continue smoke tests. Restore is
not automatic.

## 5. Health checks

```
GET /api/health          → 200, service corridoriq-sales
GET /robots.txt          → 200, text/plain
GET /sitemap.xml         → 200, application/xml, https://corridoriq.pro
GET /login.html          → 200
```

Production currently returns 404 for robots/sitemap until this branch
is what Railway is serving.

## 6–13. Smoke tests (synthetic accounts only)

Use a mailbox the operator controls. Do not email real customers.

1. Open `/` and `/explore.html` (desktop and a phone width).
2. Register a contractor → welcome dashboard, session cookie `HttpOnly`.
3. Confirm verification mail (or outbox if SMTP still off).
4. Verify the link once; reuse must fail.
5. Unverified contractor `POST /api/contractor/rfq` → 403.
6. After verify, same route → 201.
7. Register a supplier → pending; `GET /api/supplier/rfqs` → 403.
8. Verify supplier email; still 403 until admin approval.
9. Admin approves → RFQs 200; CRM `/api/sales/dashboard` still 403.
10. Existing employee login → sales landing.
11. Password reset for the synthetic contractor; old session 401.
12. Confirm `/sitemap.xml` has no welcome, admin, or `/api/` URLs.

## 14. Monitoring

Watch process logs for 5xx, mail `last_smtp_status=failed`, and 429
spikes. In-process rate limits do not share across Railway replicas.

Rollback if: migrate fails, employee login fails, cookies lack
`HttpOnly`, health is not 200, or error rate is abnormal versus the
pre-deploy baseline.

## 15. Rollback (preserve new accounts)

Prefer **application rollback** to the previous known-good deploy
artifact. Leave the database where it is.

| Keep | Do not |
| --- | --- |
| New contractor/supplier rows | Restore an older `.db` over the live file |
| Password hashes | `DROP` onboarding columns |
| `email_verified_at`, `account_state` | Replay schema “downgrade” scripts |
| Employee CRM rows | Delete sessions to “fix” auth |

Older application code ignores unknown columns. New accounts remain
valid employees/contractors on next forward deploy.

**Database restore** is only for corruption or accidental wipe:

1. Stop writers.
2. `VACUUM INTO` a panic copy of the broken file.
3. Restore a **verified** backup to a side path.
4. `PRAGMA integrity_check` on the side path.
5. Point `CORRIDORIQ_DB_PATH` at the side path, start, smoke.
6. Only then replace the live file.

Isolated restore rehearsal:

```
python scripts/restore_backup_isolated.py
```
