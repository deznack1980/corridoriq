# R2 Backup Replication — Live Verification Results

Executed 2026-09-15 against the existing implementation in
`pipeline/db/replication.py`. Rechecked 2026-09-15 04:12 local (11:12Z).
No AWS CLI was installed. No application or infrastructure code was changed.

**Outcome: credentials and local backup are ready. Off-site upload did not
complete.** Cloudflare rejected the TLS handshake on the account-specific R2
S3 endpoint before any S3 request was authenticated. Deep verification and a
clean `--status` therefore could not run.

---

## 1. Credential / status check

User-level environment variables were loaded into the verification process.
Secret values were not printed.

| Variable | Presence |
| --- | --- |
| `CORRIDORIQ_R2_ACCOUNT_ID` | SET |
| `CORRIDORIQ_R2_ACCESS_KEY_ID` | SET |
| `CORRIDORIQ_R2_SECRET_ACCESS_KEY` | SET |
| `CORRIDORIQ_R2_BUCKET` | SET (`corridoriqbackup`) |

`pipeline.db.replication.configured()` returned **True**. Derived endpoint:
`https://<account-id>.r2.cloudflarestorage.com`. Prefix: `db-backups`.
Off-site retention: 30 daily / 26 weekly.

`python -m pipeline.db.replication --status` reached R2, then failed:

```
botocore.exceptions.SSLError: SSL validation failed for
https://<account-id>.r2.cloudflarestorage.com/corridoriqbackup?list-type=2&prefix=db-backups%2F
[SSL: SSLV3_ALERT_HANDSHAKE_FAILURE] sslv3 alert handshake failure
```

That URL shape is correct: path-style S3 list against the configured bucket
and prefix. Failure is at TLS, not signing, permissions, or bucket name.

---

## 2. Newest local backup (source of the intended upload)

| Field | Value |
| --- | --- |
| File | `corridoriq_20260915T095107Z.db` |
| Written | 2026-09-15 09:51:07Z |
| Size | 414.55 MB |
| `PRAGMA integrity_check` | `ok` |
| Total rows (all user tables) | 945,023 |

This is a verified `VACUUM INTO` backup. It was **not** uploaded.

---

## 3. Upload

**Not performed.** `python -m pipeline.db.replication` would call the same
endpoint and fail the same way. Uploading was not attempted after the status
check proved TLS was down.

---

## 4. Deep verification (checksum, integrity, invariants)

**Not performed.** `--verify-deep` downloads the replica, recomputes SHA-256,
and runs SQLite `integrity_check`. There is no replica to download.

The checks that *would* have been run, once TLS works:

| Check | Command |
| --- | --- |
| SHA-256 match vs object metadata | `python -m pipeline.db.replication --verify-deep` |
| SQLite `integrity_check` on the downloaded bytes | same |
| Total row count | same (`rows` in the report) |
| Key table invariants after restore | `--restore` then query `permits`, `raw_record`, `raw_ingest_batch` |

---

## 5. Final status / unreplicated

**Cannot confirm off-site copies.** Listing objects requires the same TLS
session. Every local backup is therefore still unreplicated.

Local set at verification time:

- `corridoriq_20260915T095107Z.db` (automatic, verified)
- `pre_migration_20260915T082641Z_corridoriq.db` (manual safety copy; retention
  never replicates or prunes this class of file)

---

## TLS evidence (why this is not a client-code bug)

Python 3.14.3 / OpenSSL 3.0.18. Control hosts completed TLS 1.3:

| Host | Result |
| --- | --- |
| `google.com` | OK TLSv1.3 |
| `cloudflare.com` | OK TLSv1.3 |
| `www.cloudflare.com` | OK TLSv1.3 |
| `<account-id>.r2.cloudflarestorage.com` | **FAIL** `SSLV3_ALERT_HANDSHAKE_FAILURE` |

The same R2 host failed under TLS 1.2-only, TLS 1.3-only, and several cipher
sets. Windows `curl` (Schannel) failed with `SEC_E_ILLEGAL_MESSAGE` (fatal TLS
alert). DNS resolved to Cloudflare (`2606:4700:113::1`).

Jurisdiction probe:

| Hostname | TLS | S3 |
| --- | --- | --- |
| `<account-id>.r2.cloudflarestorage.com` (default) | fail | n/a |
| `<account-id>.{eu,wnam,enam,weur,eeur,apac}.r2...` | fail | n/a |
| `<account-id>.fedramp.r2.cloudflarestorage.com` | OK TLSv1.3 | `AccessDenied` on `ListObjectsV2` |

FedRAMP succeeding at TLS and then denying the scoped token matches a
standard (non-FedRAMP) bucket. It is not a usable workaround.

This pattern matches a known Cloudflare R2 issue: the **account-specific S3
API certificate is not serving yet** (new accounts/buckets often sit in this
state for hours). The server closes the handshake before presenting a usable
certificate. Client TLS settings cannot fix that.

SSL verification was **not** disabled. A backup that arrives over an
unauthenticated channel is not an off-site backup.

---

## Recheck — 2026-09-15 11:12Z

Same machine, same credentials, same default endpoint.

| Probe | Result |
| --- | --- |
| `curl.exe -I https://<account-id>.r2.cloudflarestorage.com/` | `SEC_E_ILLEGAL_MESSAGE` (fatal TLS alert) |
| `python -m pipeline.db.replication --status` | `SSLV3_ALERT_HANDSHAKE_FAILURE` before ListObjectsV2 |

Unchanged: no upload, no deep verify, nothing off-site. Local verified backup remains `corridoriq_20260915T095107Z.db` (414.55 MB, 945,023 rows, `integrity_check = ok`).

---

## Recheck — 2026-10-05

Same machine, Python 3.14.3, OpenSSL 3.0.18, certificate verification left on.
No proxy and no custom CA bundle. The account id is 32 hex characters and the
endpoint shape is `https://<account-id>.r2.cloudflarestorage.com`. The
configured bucket name is `corridoriqbackup`. `CORRIDORIQ_R2_ENDPOINT` is unset.

| Probe | Result |
| --- | --- |
| `cloudflare.com:443` | TLS 1.3, verification on |
| `<account-id>.r2.cloudflarestorage.com` | `SSLV3_ALERT_HANDSHAKE_FAILURE` before a certificate |
| `<account-id>.fedramp.r2.cloudflarestorage.com` | TLS 1.3, then `HeadBucket` HTTP 403 |
| DNS | Cloudflare anycast addresses |

No upload was attempted. FedRAMP is not a substitute endpoint. The standard
account certificate is still a Cloudflare-side fix.

---

## Offline implementation status (already proven)

`pipeline/tests/test_replication.py`: 21 passed against an in-memory S3
stand-in (no network, no credentials). Covered: checksum/size mismatch
detection, refusing to replicate a corrupt local file, replication failure
never breaking the local backup, `--verify-deep` catching corrupted bytes, and
restoring a replica as a queryable database.

Full suite at last run: 258 passed, 1 skipped.

---

## What to run once the endpoint certificate is live

Confirm TLS first (should return an HTTP status, not a handshake error):

```powershell
curl.exe -I https://<account-id>.r2.cloudflarestorage.com/
```

Then, from the CorridorIQ repo:

```powershell
python -m pipeline.db.replication --status
python -m pipeline.db.replication
python -m pipeline.db.replication --verify-deep
python -m pipeline.db.replication --status
```

Success looks like: upload of `corridoriq_20260915T095107Z.db` (or a newer
verified backup), `--verify-deep` reporting checksum match + `integrity_check
= ok` + ~945,023 rows, and `--status` reporting **no unreplicated local
automatic backups**.

If TLS still fails after several hours, the Cloudflare dashboard / support
path is to reprovision the R2 account S3 endpoint certificate. That is on
Cloudflare's side, not CorridorIQ's.

---

## Residual

- Local backups still share a volume with production until a replica lands in
  R2. Risk #1 is **implemented but not yet operational**.
- `boto3` is installed on this machine and is not recorded in a
  `requirements.txt` (the repo has none).
