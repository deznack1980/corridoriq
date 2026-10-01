"""Verified SQLite backups with daily/weekly retention (data platform Phase 0).

Uses ``VACUUM INTO``, which produces a consistent, defragmented copy while the
database stays online - safe to run alongside the API server or a pipeline run,
unlike a plain file copy which can capture a torn page mid-write.

Every backup is integrity-checked and row-counted immediately after it is
written. An unverified backup is not a backup, so a copy that fails validation
is deleted rather than left to be discovered during a restore.

    python -m pipeline.db.backup            # create + verify + prune
    python -m pipeline.db.backup --list     # show what exists
    python -m pipeline.db.backup --verify-latest
"""

from __future__ import annotations

import argparse
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import (
    DB_BACKUP_DIR,
    DB_BACKUP_KEEP_DAILY,
    DB_BACKUP_KEEP_WEEKLY,
    DB_PATH,
)

BACKUP_PREFIX = "corridoriq_"
_STAMP_RE = re.compile(r"^corridoriq_(\d{8}T\d{6}Z)\.db$")


def _stamp(moment: datetime | None = None) -> str:
    return (moment or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


def _parse_stamp(path: Path) -> datetime | None:
    match = _STAMP_RE.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:  # pragma: no cover - filename shaped right but invalid
        return None


def list_backups(backup_dir: Path | None = None) -> list[tuple[Path, datetime]]:
    """Automatic backups only, newest first.

    Manually retired files (``pre_migration_*``) are deliberately excluded so
    retention can never delete a hand-made safety copy.
    """
    backup_dir = backup_dir if backup_dir is not None else DB_BACKUP_DIR
    if not backup_dir.exists():
        return []
    found = []
    for path in backup_dir.glob(f"{BACKUP_PREFIX}*.db"):
        moment = _parse_stamp(path)
        if moment is not None:
            found.append((path, moment))
    return sorted(found, key=lambda pair: pair[1], reverse=True)


def verify(db_path: Path) -> tuple[bool, str, int]:
    """Return (ok, detail, total_rows) for a database file."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"cannot open: {exc}", 0
    try:
        detail = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if detail != "ok":
            return False, detail, 0
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%'"
            )
        ]
        rows = sum(
            conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables
        )
        return True, "ok", rows
    except sqlite3.DatabaseError as exc:
        return False, str(exc), 0
    finally:
        conn.close()


def create_backup(
    db_path: Path | None = None, backup_dir: Path | None = None,
    moment: datetime | None = None,
) -> Path:
    """Write a verified backup and return its path.

    Raises RuntimeError if the copy fails verification (the bad copy is
    removed first, so a failed backup never masquerades as a good one).

    Paths resolve at call time, not at import. Binding them as default
    arguments made the module impossible to redirect: anything overriding
    ``DB_BACKUP_DIR`` was silently ignored and operated on production instead.
    """
    db_path = db_path if db_path is not None else DB_PATH
    backup_dir = backup_dir if backup_dir is not None else DB_BACKUP_DIR
    if not db_path.exists():
        raise FileNotFoundError(f"no database to back up at {db_path}")

    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{BACKUP_PREFIX}{_stamp(moment)}.db"
    if target.exists():
        raise FileExistsError(f"backup already exists: {target}")

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    finally:
        conn.close()

    ok, detail, _ = verify(target)
    if not ok:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"backup failed verification and was discarded: {detail}")
    return target


def select_expired(
    backups: list[tuple[Path, datetime]],
    keep_daily: int | None = None,
    keep_weekly: int | None = None,
) -> list[Path]:
    """Backups to delete: the newest per day for `keep_daily` days and the
    newest per ISO week for `keep_weekly` weeks are retained."""
    keep_daily = DB_BACKUP_KEEP_DAILY if keep_daily is None else keep_daily
    keep_weekly = DB_BACKUP_KEEP_WEEKLY if keep_weekly is None else keep_weekly
    newest_per_day: dict[object, Path] = {}
    newest_per_week: dict[object, Path] = {}
    # `backups` is newest-first, so the first sighting of each bucket is its newest.
    for path, moment in backups:
        newest_per_day.setdefault(moment.date(), path)
        newest_per_week.setdefault(moment.isocalendar()[:2], path)

    keep = set(list(newest_per_day.values())[:keep_daily])
    keep |= set(list(newest_per_week.values())[:keep_weekly])
    return [path for path, _ in backups if path not in keep]


def prune(
    backup_dir: Path | None = None,
    keep_daily: int | None = None,
    keep_weekly: int | None = None,
) -> list[Path]:
    backup_dir = backup_dir if backup_dir is not None else DB_BACKUP_DIR
    removed = []
    for path in select_expired(list_backups(backup_dir), keep_daily, keep_weekly):
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list existing backups")
    parser.add_argument("--verify-latest", action="store_true",
                        help="integrity-check the most recent backup")
    parser.add_argument("--no-prune", action="store_true")
    args = parser.parse_args()

    if args.list:
        backups = list_backups()
        if not backups:
            print(f"No backups in {DB_BACKUP_DIR}")
            return 0
        print(f"{len(backups)} backup(s) in {DB_BACKUP_DIR}:")
        for path, moment in backups:
            size = path.stat().st_size / 1048576
            print(f"  {moment:%Y-%m-%d %H:%M:%SZ}  {size:8.2f} MB  {path.name}")
        return 0

    if args.verify_latest:
        backups = list_backups()
        if not backups:
            print("No backups to verify.")
            return 1
        path, moment = backups[0]
        ok, detail, rows = verify(path)
        print(f"{path.name}: {'OK' if ok else 'FAILED'} ({detail}), {rows:,} rows")
        return 0 if ok else 1

    print(f"Backing up {DB_PATH}")
    target = create_backup()
    ok, _, rows = verify(target)
    size = target.stat().st_size / 1048576
    print(f"  -> {target.name}  {size:.2f} MB  {rows:,} rows  verified={ok}")

    if not args.no_prune:
        removed = prune()
        if removed:
            print(f"  pruned {len(removed)} expired backup(s):")
            for path in removed:
                print(f"    - {path.name}")
        else:
            print("  nothing to prune")

    remaining = list_backups()
    print(f"  {len(remaining)} backup(s) retained in {DB_BACKUP_DIR}")

    # Off-site copy. Guarded: a backup on the local disk is still a backup, and
    # aborting over a network failure would trade a real protection for a
    # theoretical one. Silent when replication is not configured.
    from pipeline.db import replication

    if replication.configured():
        result = replication.replicate_safe()
        if result:
            verb = "already off-site" if result.get("skipped") else "replicated"
            print(f"  {verb}: {result['key']} "
                  f"({result['bytes']/1048576:.2f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
