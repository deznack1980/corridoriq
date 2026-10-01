"""Apply schema.sql and the additive column migrations to the database.

Idempotent and additive: CREATE TABLE IF NOT EXISTS plus ALTER TABLE ADD COLUMN
for columns introduced after a table already existed. No data is rewritten.

    python scripts/apply_schema.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config.settings import DB_PATH  # noqa: E402
from pipeline.db.database import apply_schema, get_connection, migrate_schema  # noqa: E402


def main() -> int:
    print(f"Applying schema to {DB_PATH}")
    conn = get_connection()
    try:
        migrate_schema(conn)
        apply_schema(conn)
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND (name LIKE 'raw_%' OR name LIKE 'source_health%') ORDER BY name"
            )
        ]
        print(f"data-platform tables: {', '.join(tables) or '(none)'}")
        for table in ("raw_record", "ingestion_runs", "source_health_snapshot"):
            cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")]
            if cols:
                print(f"\n{table} ({len(cols)} columns):\n  {', '.join(cols)}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
