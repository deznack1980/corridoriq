# Off-site Backup Replication to Cloudflare R2

Closes **risk #1** from the Phase 2 closeout: local backups lived on the same
volume as the database.

```
C:\CorridorIQData\db\corridoriq.db        <- production
C:\CorridorIQData\backups\*.db            <- every backup
```

Retention was never the problem (14 daily / 8 weekly, correctly configured).
The *location* was. One drive failure would have destroyed production and every
copy of it simultaneously. Replication to R2 puts the copies in a second
failure domain.

---

## What it does

After each local backup succeeds, the newest verified backup is uploaded to R2
and the remote copy is checked against the local one. Off-site retention is
deeper than local — 30 daily / 26 weekly — because the off-site copy is the one
that matters when the local disk is gone, and R2 is cheap enough that depth
costs little.

### Three properties worth knowing

**Replication can never fail a backup.** If the network is down, the upload is
skipped with a warning and the local backup still completes. A backup on the
local disk is a real protection; aborting the job over a network error would
trade it for a theoretical one.

**Verification does not trust the upload.** R2 returns an ETag, but for a
multipart upload (which a 415 MB file always is) that is a digest of part
digests, not of the file — it cannot be compared to a local checksum. Instead
the SHA-256 is computed locally, attached as object metadata, and re-read from
the remote object after upload, along with a byte-for-byte size comparison. A
mismatch raises rather than reporting success.

**Only `--verify-deep` proves the bytes are good.** Metadata can be correct
while the object is not. `--verify-deep` downloads the replica, recomputes the
SHA-256, and runs SQLite's integrity check against it. That is the only check
that proves what is in R2 is a working database. Worth running monthly.

---

## One-time setup

### 1. Install the client

```powershell
python -m pip install boto3
```

> The project has no `requirements.txt`, so this dependency is not recorded
> anywhere. Noted as a gap — if the pipeline is ever set up on another machine,
> this install is a manual prerequisite.

### 2. Create the bucket

In the Cloudflare dashboard: **R2 → Create bucket**.

- Name: `corridoriqbackup` (this is the configured bucket; older notes that say `corridoriq-backups` are stale)
- Location: automatic
- Leave public access **disabled**. These are database backups; they must not
  be reachable without credentials.

### 3. Create a scoped API token

**R2 → Manage R2 API Tokens → Create API Token**.

- Permission: **Object Read & Write** (not Admin)
- Scope it to the `corridoriqbackup` bucket only
- Save the **Access Key ID** and **Secret Access Key** — the secret is shown
  once

Scoping matters: a token limited to one bucket with object-level permissions
cannot delete the bucket, create others, or read anything else in the account.

### 4. Set the environment variables

Credentials come from the environment and are never stored in the repository.
`setx` persists them for the user account, which is what the scheduled task
runs as:

```powershell
setx CORRIDORIQ_R2_ACCOUNT_ID      "<cloudflare account id>"
setx CORRIDORIQ_R2_ACCESS_KEY_ID   "<access key id>"
setx CORRIDORIQ_R2_SECRET_ACCESS_KEY "<secret access key>"
setx CORRIDORIQ_R2_BUCKET          "corridoriqbackup"
```

`setx` affects **new** processes only — open a fresh terminal afterwards.

The account ID is the hex string in your R2 endpoint,
`https://<account-id>.r2.cloudflarestorage.com`. That is the only endpoint
this application uses. The bucket `corridoriqbackup` already exists; do not
create a second one.

As of 2026-10-05 the standard account endpoint closes the TLS handshake with
`SSLV3_ALERT_HANDSHAKE_FAILURE` before any HTTP request. `cloudflare.com`
completes TLS 1.3 from the same machine, with certificate verification left
on. The FedRAMP hostname presents a certificate and then returns HTTP 403 for
the current token. Do not switch replication to FedRAMP, and do not disable
certificate verification. Off-machine backup stays blocked until Cloudflare
serves a certificate for the standard account endpoint.

### 5. Confirm

```powershell
python -m pipeline.db.replication --status
```

Unset credentials disable replication rather than breaking anything; the
command reports exactly which variables are missing.

---

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `CORRIDORIQ_R2_ACCOUNT_ID` | — | Cloudflare account, forms the endpoint |
| `CORRIDORIQ_R2_ACCESS_KEY_ID` | — | R2 API token key |
| `CORRIDORIQ_R2_SECRET_ACCESS_KEY` | — | R2 API token secret |
| `CORRIDORIQ_R2_BUCKET` | — | Target bucket |
| `CORRIDORIQ_R2_PREFIX` | `db-backups` | Key prefix inside the bucket |
| `CORRIDORIQ_R2_REPLICATION` | `1` | Set `0` to disable while keeping credentials |
| `CORRIDORIQ_R2_KEEP_DAILY` | `30` | Off-site daily retention |
| `CORRIDORIQ_R2_KEEP_WEEKLY` | `26` | Off-site weekly retention |
| `CORRIDORIQ_R2_CHUNK_BYTES` | `33554432` | Multipart part size (32 MB) |
| `CORRIDORIQ_R2_ENDPOINT` | derived | Override for testing |

---

## Commands

| Task | Command |
| --- | --- |
| Backup + replicate (the normal path) | `python -m pipeline.db.backup` |
| Replicate newest backup only | `python -m pipeline.db.replication` |
| Local vs off-site comparison | `python -m pipeline.db.replication --status` |
| List replicas | `python -m pipeline.db.replication --list` |
| **End-to-end verification** | `python -m pipeline.db.replication --verify-deep` |
| Download a replica | `python -m pipeline.db.replication --restore <name> --dest <path>` |

`--status` exits non-zero when any local backup lacks an off-site copy, so it
works as a scheduled check.

---

## Disaster recovery

If the machine or its disk is lost:

1. Install Python and `boto3` on the replacement machine.
2. Set the four R2 environment variables (step 4 above).
3. List what survived:

   ```powershell
   python -m pipeline.db.replication --list
   ```

4. Restore the newest replica straight into place:

   ```powershell
   python -m pipeline.db.replication --restore corridoriq_<stamp>.db `
       --dest C:\CorridorIQData\db\corridoriq.db
   ```

   The command integrity-checks and row-counts the downloaded file, and exits
   non-zero if it does not open cleanly.

5. Confirm the restored database:

   ```powershell
   python -m pipeline.db.doctor
   ```

Restoring a backup taken with `VACUUM INTO` needs no replay or repair — it is
a complete, consistent database file.

---

## Cost

R2 charges for storage and operations but **not egress**, which matters here:
restoring a 415 MB database costs nothing to download.

At current size, 30 daily plus 26 weekly copies is roughly 23 GB, about
**$0.35/month** at $0.015/GB-month. One upload per day is negligible against
Class A operation pricing. Note this exceeds R2's 10 GB free tier, so expect a
small real bill rather than zero.

The database grows as the RAW layer accumulates, and replication cost grows
with it linearly. At 1 GB the same policy would cost roughly $0.85/month.

---

## What this does not protect against

- **Ransomware or accidental deletion propagating off-site.** The token has
  write and delete rights so retention can prune, which means a compromised
  machine could delete replicas. R2 object versioning or a bucket lock would
  close this; neither is configured.
- **A corrupted database being faithfully replicated.** Backups are
  integrity-checked before upload, so structural corruption is caught — but
  logically wrong data backs up perfectly well.
- **Credential loss.** If the R2 token is lost and the machine is gone, the
  replicas are unreachable. Store the account ID and token somewhere separate
  from the machine being protected.

---

## Tests

21 tests in `pipeline/tests/test_replication.py`, using an in-memory stand-in
for the S3 API so they run with no credentials and no network. They cover the
properties that decide whether the off-site copy can be trusted: truncated
uploads and checksum mismatches are detected rather than reported as success,
an unverified local backup is never replicated, replication failure never
breaks the backup that already succeeded, corruption in transit is caught by
`--verify-deep`, and a replica can be downloaded and queried as a database.

### One defect found while building this

The test that runs the full backup job initially operated on **production**
despite pointing at a temporary directory, and pruned a real backup. The cause
was in `pipeline/db/backup.py`: `DB_PATH` and `DB_BACKUP_DIR` were bound as
default arguments, which Python evaluates once at import, so overriding the
module attributes had no effect. Those paths now resolve at call time.

No data was lost — the pruned file was replaced by an equally valid verified
backup — but a backup module that silently ignores attempts to redirect it is
dangerous in its own right, and there is now a regression test for it.
