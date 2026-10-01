"""Off-site backup replication to Cloudflare R2.

Closeout risk #1: local backups sat on the same volume as the database, so a
single drive failure would have destroyed production and every backup at once.
Retention was never the problem; location was. This module copies verified
local backups into R2, giving them a second failure domain.

Design notes:

* **Replication never fails a backup.** `replicate_safe()` swallows everything.
  A local backup that exists is worth more than a run that aborted because the
  network was down.
* **Verification does not trust the upload.** R2 returns an ETag, but for a
  multipart upload that is a digest of part digests, not of the file -- it
  cannot be compared against a local checksum. Instead the SHA-256 is computed
  locally, stored as object metadata, and re-read from the remote object after
  upload. `--verify-deep` goes further and downloads the object back to recompute
  the hash and run SQLite's integrity check against it, which is the only check
  that proves the bytes in R2 are a working database.
* **Credentials come from the environment**, never the repository, and their
  absence disables replication rather than breaking the backup.

    python -m pipeline.db.replication                  # replicate newest backup
    python -m pipeline.db.replication --list
    python -m pipeline.db.replication --verify-deep
    python -m pipeline.db.replication --restore <name> --dest <path>
    python -m pipeline.db.replication --status
"""

from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config import settings
from pipeline.db.backup import _parse_stamp, list_backups, verify

CHECKSUM_META = "sha256"
ROWS_META = "rows"
SOURCE_META = "source-host"


class ReplicationNotConfigured(RuntimeError):
    """Raised when R2 credentials are absent and an operation needs them."""


def configured() -> bool:
    return settings.R2_REPLICATION_ENABLED


def sha256_of(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def build_client():
    """An S3 client pointed at this account's R2 endpoint."""
    if not configured():
        raise ReplicationNotConfigured(
            "R2 replication is not configured. Set CORRIDORIQ_R2_ACCOUNT_ID, "
            "CORRIDORIQ_R2_ACCESS_KEY_ID, CORRIDORIQ_R2_SECRET_ACCESS_KEY and "
            "CORRIDORIQ_R2_BUCKET."
        )
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - dependency is installed
        raise ReplicationNotConfigured(
            "boto3 is required for R2 replication: python -m pip install boto3"
        ) from exc

    return boto3.client(
        "s3",
        endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        # R2 has no regions but SigV4 requires one; 'auto' is Cloudflare's.
        region_name="auto",
        config=Config(signature_version="s3v4", retries={"max_attempts": 3,
                                                         "mode": "standard"}),
    )


def _transfer_config():
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(
        multipart_threshold=settings.R2_MULTIPART_CHUNK_BYTES,
        multipart_chunksize=settings.R2_MULTIPART_CHUNK_BYTES,
        max_concurrency=4,
        use_threads=True,
    )


def remote_key(name: str) -> str:
    prefix = settings.R2_PREFIX
    return f"{prefix}/{name}" if prefix else name


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------

def upload(path: Path, *, client=None, bucket: str | None = None) -> dict:
    """Upload one backup and confirm the remote object matches.

    Returns a result dict. Raises RuntimeError if the remote object does not
    match the local file, so a mismatch is never mistaken for a success.
    """
    client = client or build_client()
    bucket = bucket or settings.R2_BUCKET
    key = remote_key(path.name)

    local_size = path.stat().st_size
    digest = sha256_of(path)
    ok, detail, rows = verify(path)
    if not ok:
        raise RuntimeError(f"refusing to replicate an unverified backup: {detail}")

    import socket

    client.upload_file(
        str(path), bucket, key,
        ExtraArgs={"Metadata": {CHECKSUM_META: digest,
                                ROWS_META: str(rows),
                                SOURCE_META: socket.gethostname()}},
        Config=_transfer_config(),
    )

    head = client.head_object(Bucket=bucket, Key=key)
    remote_size = head.get("ContentLength")
    remote_digest = (head.get("Metadata") or {}).get(CHECKSUM_META)

    if remote_size != local_size:
        raise RuntimeError(
            f"size mismatch after upload: local {local_size} != remote {remote_size}")
    if remote_digest != digest:
        raise RuntimeError(
            f"checksum metadata mismatch after upload for {key}")

    return {"key": key, "bytes": local_size, "sha256": digest, "rows": rows}


def replicate_latest(*, client=None, bucket: str | None = None) -> dict | None:
    """Upload the newest local backup unless an identical object already exists."""
    backups = list_backups()
    if not backups:
        return None
    path, _ = backups[0]
    client = client or build_client()
    bucket = bucket or settings.R2_BUCKET
    key = remote_key(path.name)

    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except Exception:  # noqa: BLE001 - any miss means "not there yet"
        head = None

    if head is not None and head.get("ContentLength") == path.stat().st_size:
        return {"key": key, "bytes": head["ContentLength"], "skipped": True,
                "sha256": (head.get("Metadata") or {}).get(CHECKSUM_META),
                "rows": int((head.get("Metadata") or {}).get(ROWS_META, 0) or 0)}

    result = upload(path, client=client, bucket=bucket)
    result["skipped"] = False
    return result


def replicate_safe() -> dict | None:
    """replicate_latest() that can never fail the caller.

    A backup that exists locally but failed to replicate is still a backup.
    Aborting the backup job over a network problem would trade a real
    protection for a theoretical one.
    """
    if not configured():
        return None
    try:
        return replicate_latest()
    except Exception as exc:  # noqa: BLE001
        print(f"  off-site replication FAILED (local backup is unaffected): {exc}")
        return None


# ---------------------------------------------------------------------------
# Inspect, verify, prune, restore
# ---------------------------------------------------------------------------

def list_remote(*, client=None, bucket: str | None = None) -> list[dict]:
    """Replicated backups, newest first."""
    client = client or build_client()
    bucket = bucket or settings.R2_BUCKET
    prefix = f"{settings.R2_PREFIX}/" if settings.R2_PREFIX else ""

    found: list[dict] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for obj in page.get("Contents", []):
            name = obj["Key"].rsplit("/", 1)[-1]
            moment = _parse_stamp(Path(name))
            if moment is None:
                continue
            found.append({"key": obj["Key"], "name": name, "moment": moment,
                          "bytes": obj.get("Size", 0)})
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    return sorted(found, key=lambda d: d["moment"], reverse=True)


def download(name: str, dest: Path, *, client=None, bucket: str | None = None) -> Path:
    client = client or build_client()
    bucket = bucket or settings.R2_BUCKET
    dest.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(bucket, remote_key(name), str(dest),
                         Config=_transfer_config())
    return dest


def verify_deep(name: str | None = None, *, client=None,
                bucket: str | None = None) -> dict:
    """Download a replicated backup and prove it is a working database.

    Metadata comparison shows the upload was recorded correctly; only reading
    the bytes back shows they survived. This recomputes the SHA-256 and runs
    SQLite's integrity check on the downloaded copy.
    """
    client = client or build_client()
    bucket = bucket or settings.R2_BUCKET

    remote = list_remote(client=client, bucket=bucket)
    if not remote:
        raise RuntimeError("no replicated backups to verify")
    entry = next((r for r in remote if r["name"] == name), None) if name else remote[0]
    if entry is None:
        raise RuntimeError(f"no replicated backup named {name}")

    head = client.head_object(Bucket=bucket, Key=entry["key"])
    expected = (head.get("Metadata") or {}).get(CHECKSUM_META)

    scratch = Path(tempfile.gettempdir()) / f"ciq_r2_verify_{entry['name']}"
    try:
        download(entry["name"], scratch, client=client, bucket=bucket)
        actual = sha256_of(scratch)
        ok, detail, rows = verify(scratch)
        return {
            "name": entry["name"],
            "bytes": scratch.stat().st_size,
            "sha256_expected": expected,
            "sha256_actual": actual,
            "checksum_match": bool(expected) and expected == actual,
            "integrity": detail,
            "integrity_ok": ok,
            "rows": rows,
        }
    finally:
        scratch.unlink(missing_ok=True)


def select_expired_remote(entries: list[dict], keep_daily: int, keep_weekly: int) -> list[dict]:
    """Same daily/weekly policy as local retention, applied to remote keys."""
    newest_per_day: dict[object, str] = {}
    newest_per_week: dict[object, str] = {}
    for entry in entries:  # newest first
        newest_per_day.setdefault(entry["moment"].date(), entry["key"])
        newest_per_week.setdefault(entry["moment"].isocalendar()[:2], entry["key"])

    keep = set(list(newest_per_day.values())[:keep_daily])
    keep |= set(list(newest_per_week.values())[:keep_weekly])
    return [e for e in entries if e["key"] not in keep]


def prune_remote(*, client=None, bucket: str | None = None,
                 keep_daily: int | None = None,
                 keep_weekly: int | None = None) -> list[str]:
    client = client or build_client()
    bucket = bucket or settings.R2_BUCKET
    entries = list_remote(client=client, bucket=bucket)
    expired = select_expired_remote(
        entries,
        settings.R2_KEEP_DAILY if keep_daily is None else keep_daily,
        settings.R2_KEEP_WEEKLY if keep_weekly is None else keep_weekly,
    )
    for entry in expired:
        client.delete_object(Bucket=bucket, Key=entry["key"])
    return [e["name"] for e in expired]


def status(*, client=None, bucket: str | None = None) -> dict:
    """Compare local and remote backup sets."""
    local = [{"name": p.name, "moment": m, "bytes": p.stat().st_size}
             for p, m in list_backups()]
    if not configured():
        return {"configured": False, "local": local, "remote": [],
                "unreplicated": [b["name"] for b in local]}
    remote = list_remote(client=client, bucket=bucket)
    remote_names = {r["name"] for r in remote}
    return {
        "configured": True,
        "bucket": bucket or settings.R2_BUCKET,
        "endpoint": settings.R2_ENDPOINT_URL,
        "prefix": settings.R2_PREFIX,
        "local": local,
        "remote": remote,
        "unreplicated": [b["name"] for b in local if b["name"] not in remote_names],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _mb(n) -> str:
    return f"{(n or 0)/1048576:,.2f} MB"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list replicated backups")
    parser.add_argument("--status", action="store_true",
                        help="compare local and off-site backup sets")
    parser.add_argument("--verify-deep", action="store_true",
                        help="download the newest replica and re-check it end to end")
    parser.add_argument("--restore", metavar="NAME", help="download a replica")
    parser.add_argument("--dest", metavar="PATH", help="destination for --restore")
    parser.add_argument("--no-prune", action="store_true")
    args = parser.parse_args()

    if not configured():
        print("R2 replication is not configured.")
        print("  Required environment variables:")
        for name in ("CORRIDORIQ_R2_ACCOUNT_ID", "CORRIDORIQ_R2_ACCESS_KEY_ID",
                     "CORRIDORIQ_R2_SECRET_ACCESS_KEY", "CORRIDORIQ_R2_BUCKET"):
            import os
            print(f"    {name:<34}{'set' if os.environ.get(name) else 'MISSING'}")
        print("  See docs/operations/offsite_backup_r2.md")
        return 1

    if args.status:
        info = status()
        print(f"endpoint : {info['endpoint']}")
        print(f"bucket   : {info['bucket']}/{info['prefix']}")
        print(f"\nlocal  : {len(info['local'])} backup(s)")
        for b in info["local"]:
            print(f"  {b['moment']:%Y-%m-%d %H:%M:%SZ}  {_mb(b['bytes']):>12}  {b['name']}")
        print(f"\noff-site: {len(info['remote'])} backup(s)")
        for r in info["remote"]:
            print(f"  {r['moment']:%Y-%m-%d %H:%M:%SZ}  {_mb(r['bytes']):>12}  {r['name']}")
        if info["unreplicated"]:
            print(f"\nNOT replicated: {', '.join(info['unreplicated'])}")
            return 1
        print("\nEvery local backup has an off-site copy.")
        return 0

    if args.list:
        remote = list_remote()
        if not remote:
            print("No replicated backups.")
            return 0
        print(f"{len(remote)} replicated backup(s) in "
              f"{settings.R2_BUCKET}/{settings.R2_PREFIX}:")
        for r in remote:
            print(f"  {r['moment']:%Y-%m-%d %H:%M:%SZ}  {_mb(r['bytes']):>12}  {r['name']}")
        return 0

    if args.verify_deep:
        print("Downloading newest replica for end-to-end verification ...")
        result = verify_deep()
        print(f"  name            : {result['name']}")
        print(f"  size            : {_mb(result['bytes'])}")
        print(f"  sha256 expected : {result['sha256_expected']}")
        print(f"  sha256 actual   : {result['sha256_actual']}")
        print(f"  checksum match  : {result['checksum_match']}")
        print(f"  integrity_check : {result['integrity']}")
        print(f"  rows            : {result['rows']:,}")
        good = result["checksum_match"] and result["integrity_ok"]
        print(f"\n  {'PASS' if good else 'FAIL'}")
        return 0 if good else 1

    if args.restore:
        dest = Path(args.dest) if args.dest else Path.cwd() / args.restore
        print(f"Downloading {args.restore} -> {dest}")
        download(args.restore, dest)
        ok, detail, rows = verify(dest)
        print(f"  {_mb(dest.stat().st_size)}  integrity={detail}  rows={rows:,}")
        return 0 if ok else 1

    result = replicate_latest()
    if result is None:
        print("No local backups to replicate.")
        return 1
    verb = "already present" if result.get("skipped") else "uploaded"
    print(f"Off-site replication to {settings.R2_BUCKET}/{settings.R2_PREFIX}")
    print(f"  {verb}: {result['key']}  {_mb(result['bytes'])}  "
          f"rows={result.get('rows', 0):,}")
    if result.get("sha256"):
        print(f"  sha256: {result['sha256']}")

    if not args.no_prune:
        removed = prune_remote()
        if removed:
            print(f"  pruned {len(removed)} expired replica(s):")
            for name in removed:
                print(f"    - {name}")
        else:
            print("  nothing to prune off-site")

    print(f"  {len(list_remote())} replica(s) retained off-site")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
