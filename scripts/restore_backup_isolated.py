"""Copy the newest local backup into a temp folder and verify it.

Read-only on the source. Never writes to the production database path.

    python scripts/restore_backup_isolated.py
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from pathlib import Path

from pipeline.db.backup import list_backups, verify


def main() -> int:
    backups = list_backups()
    if not backups:
        print("NO_LOCAL_BACKUPS")
        return 2
    src, moment = backups[0]
    dest_dir = Path(tempfile.mkdtemp(prefix="ciq-restore-"))
    dest = dest_dir / src.name
    shutil.copy2(src, dest)
    ok, detail, rows = verify(dest)
    conn = sqlite3.connect(f"file:{dest.as_posix()}?mode=ro", uri=True)
    tables = {
        r[0]
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    required = {"users", "roles", "sessions", "organizations"}
    user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    print(f"source_stamp={moment.isoformat()}")
    print(f"integrity={detail}")
    print(f"rows={rows}")
    print(f"required_tables={sorted(required & tables)}")
    print(f"missing={sorted(required - tables)}")
    print(f"users_rows={user_count}")
    print(f"ok={ok}")
    return 0 if ok and not (required - tables) else 1


if __name__ == "__main__":
    raise SystemExit(main())
