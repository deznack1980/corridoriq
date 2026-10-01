"""Move the operational database out of the repo (and out of OneDrive).

Data-platform Phase 0. The database must not sit in a cloud-synced folder: a
multi-hundred-MB SQLite file under continuous sync, with a scheduled writer and
a long-lived server process, risks partial-file sync and journal/WAL divergence.

The move is verified before anything is given up:

    1. integrity_check the source
    2. record a manifest of every table and its row count
    3. copy with VACUUM INTO (clean, defragmented, refuses to overwrite)
    4. integrity_check the copy and compare the manifest row for row
    5. only then switch the copy to WAL and retire the original

The original is never deleted. It is moved into the backups directory as a
pre-migration snapshot, which also removes it from the synced folder.

Usage:
    python scripts/migrate_database_location.py [--dry-run] [--keep-original]
    python scripts/migrate_database_location.py --source PATH --target PATH
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config.settings import (  # noqa: E402
    DB_BACKUP_DIR,
    DB_PATH,
    LEGACY_DB_PATH,
)

SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "icloud", "box sync")


def _log(message: str) -> None:
    print(message, flush=True)


def _is_synced(path: Path) -> bool:
    lowered = str(path).lower()
    return any(marker in lowered for marker in SYNC_MARKERS)


@contextmanager
def _open_ro(path: Path):
    """Read-only connection that is actually CLOSED on exit.

    `with sqlite3.connect(...)` only scopes a transaction - it leaves the
    connection (and the file handle) open, which on Windows blocks the very
    move this script exists to perform.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def manifest(conn: sqlite3.Connection) -> dict[str, int]:
    """Table name -> row count, for every user table."""
    tables = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    return {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}


def integrity_ok(conn: sqlite3.Connection) -> tuple[bool, str]:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    return result == "ok", result


def compare(before: dict[str, int], after: dict[str, int]) -> list[str]:
    problems = []
    for table in sorted(set(before) | set(after)):
        old, new = before.get(table), after.get(table)
        if old != new:
            problems.append(f"  {table}: source={old} target={new}")
    return problems


def _retire(source: Path, *, attempts: int = 5, delay: float = 3.0) -> int:
    """Move the source out of the synced folder into the backups directory.

    A cloud-sync client will intermittently hold the file open (exactly the
    hazard this relocation exists to remove), so the move is retried before
    giving up, and failure leaves the original safely in place.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    DB_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    retired = DB_BACKUP_DIR / f"pre_migration_{stamp}_{source.name}"

    for attempt in range(1, attempts + 1):
        try:
            source.replace(retired)
            break
        except PermissionError as exc:
            if attempt == attempts:
                _log(f"      could not retire the original: {exc}")
                _log(f"      it is still at {source}")
                _log("      Something (usually the sync client) is holding it open.")
                _log("      The new database is already live and verified; re-run")
                _log("      this script with --retire-only when the file is free.")
                return 1
            _log(f"      locked, retrying in {delay:.0f}s ({attempt}/{attempts - 1}) ...")
            time.sleep(delay)

    _log(f"      moved to {retired}")
    for suffix in ("-journal", "-wal", "-shm"):
        sibling = source.with_name(source.name + suffix)
        if sibling.exists():
            sibling.replace(retired.with_name(retired.name + suffix))
            _log(f"      moved to {retired.name + suffix}")
    return 0


def migrate(source: Path, target: Path, *, dry_run: bool, keep_original: bool,
            retire_only: bool = False) -> int:
    _log("=" * 66)
    _log(" CorridorIQ - database relocation (Phase 0)")
    _log("=" * 66)

    if not source.exists():
        if retire_only:
            _log(f"Nothing to retire: {source} no longer exists.")
            return 0
        _log(f"FAIL: no database at source: {source}")
        return 1

    if retire_only:
        if not target.exists():
            _log(f"FAIL: refusing to retire the source - no database at target:\n  {target}")
            return 1
        with _open_ro(target) as conn:
            ok, detail = integrity_ok(conn)
            if not ok:
                _log(f"FAIL: target failed integrity_check: {detail}")
                return 1
            rows = sum(manifest(conn).values())
        _log(f"target verified: {target} ({rows:,} rows)")
        _log("\nretiring the original ...")
        return _retire(source)
    if source.resolve() == target.resolve():
        _log(f"Nothing to do: source and target are the same path.\n  {source}")
        return 0
    if target.exists():
        _log(f"FAIL: target already exists, refusing to overwrite:\n  {target}")
        _log("Move or remove it first, or pass an explicit --target.")
        return 1

    size_mb = source.stat().st_size / 1048576
    _log(f"source : {source}")
    _log(f"         {size_mb:.2f} MB" + ("  [CLOUD-SYNCED]" if _is_synced(source) else ""))
    _log(f"target : {target}" + ("  [CLOUD-SYNCED]" if _is_synced(target) else ""))

    if _is_synced(target):
        _log("\nFAIL: the target is itself inside a cloud-synced folder.")
        _log("Set CORRIDORIQ_DATA_DIR to a local, unsynced location.")
        return 1

    _log("\n[1/5] integrity_check on source ...")
    with _open_ro(source) as conn:
        ok, detail = integrity_ok(conn)
        if not ok:
            _log(f"FAIL: source failed integrity_check: {detail}")
            _log("Do NOT migrate a corrupt database. Restore from backup first.")
            return 1
        before = manifest(conn)
    total_rows = sum(before.values())
    _log(f"      ok - {len(before)} tables, {total_rows:,} rows")

    if dry_run:
        _log("\n[dry-run] would VACUUM INTO the target, verify, then retire the source.")
        _log("[dry-run] no changes made.")
        return 0

    _log(f"\n[2/5] copying via VACUUM INTO ...")
    target.parent.mkdir(parents=True, exist_ok=True)
    with _open_ro(source) as conn:
        conn.execute("VACUUM INTO ?", (str(target),))
    new_mb = target.stat().st_size / 1048576
    _log(f"      wrote {new_mb:.2f} MB (reclaimed {size_mb - new_mb:.2f} MB)")

    _log("\n[3/5] verifying the copy ...")
    with _open_ro(target) as conn:
        ok, detail = integrity_ok(conn)
        if not ok:
            _log(f"FAIL: target failed integrity_check: {detail}")
            _log(f"Source is untouched at {source}. Delete the bad copy and retry.")
            return 1
        after = manifest(conn)

    problems = compare(before, after)
    if problems:
        _log("FAIL: row counts do not match:")
        for line in problems:
            _log(line)
        _log(f"\nSource is untouched at {source}. Delete the bad copy and retry.")
        return 1
    _log(f"      ok - {len(after)} tables match, {sum(after.values()):,} rows")

    _log("\n[4/5] enabling WAL on the new database ...")
    conn = sqlite3.connect(target)
    mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.commit()
    conn.close()
    _log(f"      journal_mode = {mode}")

    _log("\n[5/5] retiring the original ...")
    if keep_original:
        _log(f"      --keep-original: left in place at {source}")
        _log("      NOTE: it is still inside the synced folder.")
    elif _retire(source) != 0:
        return 1

    _log("\n" + "=" * 66)
    _log(" Relocation complete.")
    _log("=" * 66)
    _log(f"Operational database : {target}")
    _log("Verify with          : python -m pipeline.db.doctor")
    if not keep_original:
        _log(f"Pre-migration copy   : {DB_BACKUP_DIR}")
        _log("Keep that copy until you have confirmed a few good pipeline runs.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=LEGACY_DB_PATH)
    parser.add_argument("--target", type=Path, default=DB_PATH)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-original", action="store_true",
                        help="do not retire the source (leaves it in the synced folder)")
    parser.add_argument("--retire-only", action="store_true",
                        help="target already migrated; just retire the source file")
    args = parser.parse_args()
    return migrate(args.source, args.target,
                   dry_run=args.dry_run, keep_original=args.keep_original,
                   retire_only=args.retire_only)


if __name__ == "__main__":
    raise SystemExit(main())
