"""Operational health check for the CorridorIQ database.

Verifies the things Phase 0 was supposed to guarantee, and actually exercises
the writer rather than only reading settings - a database that reports WAL but
cannot commit a transaction is still broken.

    python -m pipeline.db.doctor

Exit code 0 = all checks passed, 1 = at least one FAIL.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from pipeline.config.settings import (
    DB_BACKUP_DIR,
    DB_PATH,
    LEGACY_DB_PATH,
    RAW_CAPTURE_ENABLED,
)
from pipeline.db.backup import list_backups, verify
from pipeline.db.database import get_connection

SYNC_MARKERS = ("onedrive", "dropbox", "google drive", "icloud", "box sync")

_PASS, _WARN, _FAIL = "PASS", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def _check(level: str, name: str, detail: str) -> None:
    _results.append((level, name, detail))
    print(f"  [{level}] {name}: {detail}")


def _is_synced(path: Path) -> bool:
    return any(marker in str(path).lower() for marker in SYNC_MARKERS)


def run() -> int:
    print("=" * 66)
    print(" CorridorIQ - database doctor")
    print("=" * 66)

    print("\nLocation")
    if not DB_PATH.exists():
        _check(_FAIL, "database file", f"missing at {DB_PATH}")
        print("\nCannot continue without a database.")
        return 1
    size_mb = DB_PATH.stat().st_size / 1048576
    _check(_PASS, "database file", f"{DB_PATH} ({size_mb:.2f} MB)")

    if _is_synced(DB_PATH):
        _check(_FAIL, "cloud sync", "database is inside a cloud-synced folder")
    else:
        _check(_PASS, "cloud sync", "outside any synced folder")

    if LEGACY_DB_PATH.exists():
        _check(_WARN, "legacy copy",
               f"an old in-repo database still exists at {LEGACY_DB_PATH}")
    else:
        _check(_PASS, "legacy copy", "no stale in-repo database")

    print("\nConnection")
    try:
        conn = get_connection()
    except Exception as exc:  # noqa: BLE001
        _check(_FAIL, "connect", str(exc))
        return 1

    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        _check(_PASS if journal.lower() == "wal" else _FAIL,
               "journal_mode", journal)

        fk = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        _check(_PASS if fk else _FAIL, "foreign_keys",
               "ON (per-connection; SQLite has no persistent setting)" if fk else "OFF")

        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        _check(_PASS if busy >= 5000 else _WARN, "busy_timeout", f"{busy} ms")

        sync = conn.execute("PRAGMA synchronous").fetchone()[0]
        _check(_PASS, "synchronous", {0: "OFF", 1: "NORMAL", 2: "FULL"}.get(sync, str(sync)))

        print("\nIntegrity")
        detail = conn.execute("PRAGMA integrity_check").fetchone()[0]
        _check(_PASS if detail == "ok" else _FAIL, "integrity_check", detail)

        violations = list(conn.execute("PRAGMA foreign_key_check"))
        _check(_PASS if not violations else _FAIL, "foreign_key_check",
               "0 violations" if not violations else f"{len(violations)} violations")

        print("\nWriter")
        # Actually commit and roll back through a real transaction: the point
        # is to prove the file is writable and not lock-starved, not to trust
        # a pragma. A temp table is used so no production table is touched.
        try:
            conn.execute("CREATE TEMP TABLE _doctor_probe (v INTEGER)")
            conn.execute("INSERT INTO _doctor_probe VALUES (1)")
            conn.commit()
            value = conn.execute("SELECT v FROM _doctor_probe").fetchone()[0]
            conn.execute("DROP TABLE _doctor_probe")
            conn.commit()
            _check(_PASS if value == 1 else _FAIL, "write probe",
                   "committed and rolled back a transaction")
        except sqlite3.Error as exc:
            _check(_FAIL, "write probe", str(exc))

        print("\nContent")
        for table in ("permits", "projects", "companies"):
            try:
                n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                _check(_PASS, f"{table} rows", f"{n:,}")
            except sqlite3.Error as exc:
                _check(_FAIL, f"{table} rows", str(exc))

        has_raw = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='raw_record'"
        ).fetchone()
        if has_raw:
            versions = conn.execute("SELECT COUNT(*) FROM raw_record").fetchone()[0]
            records = conn.execute(
                "SELECT COUNT(*) FROM raw_record WHERE is_current = 1"
            ).fetchone()[0]
            _check(_PASS, "raw capture",
                   f"{records:,} records, {versions:,} versions "
                   f"({'enabled' if RAW_CAPTURE_ENABLED else 'DISABLED by env'})")
        else:
            _check(_WARN, "raw capture", "raw_record table not present")
    finally:
        conn.close()

    print("\nBackups")
    backups = list_backups()
    if not backups:
        _check(_FAIL, "backups", f"none found in {DB_BACKUP_DIR}")
    else:
        newest, moment = backups[0]
        ok, detail, rows = verify(newest)
        _check(_PASS if ok else _FAIL, "newest backup",
               f"{newest.name} ({moment:%Y-%m-%d %H:%MZ}, {rows:,} rows, {detail})")
        _check(_PASS, "backup count", f"{len(backups)} retained")

    print("\n" + "=" * 66)
    fails = [r for r in _results if r[0] == _FAIL]
    warns = [r for r in _results if r[0] == _WARN]
    if fails:
        print(f" {len(fails)} FAIL, {len(warns)} WARN")
        for _, name, detail in fails:
            print(f"   FAIL {name}: {detail}")
    else:
        print(f" All checks passed ({len(warns)} warning(s))")
    print("=" * 66)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
