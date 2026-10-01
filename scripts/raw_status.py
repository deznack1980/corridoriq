"""Read-only snapshot of the RAW capture layer.

Diagnostics for data platform Phase 1: how much history exists, which sources
are genuinely changing, and which are churning.

    python scripts/raw_status.py
"""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config.settings import DB_PATH  # noqa: E402


def main() -> int:
    with closing(sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row

        print("=" * 72)
        print(" RAW capture layer")
        print("=" * 72)

        row = conn.execute(
            "SELECT COUNT(*) v, COUNT(DISTINCT source_system || '|' || source_record_id) r,"
            " MIN(fetched_at) lo, MAX(fetched_at) hi FROM raw_record"
        ).fetchone()
        print(f"versions : {row['v']:,}")
        print(f"records  : {row['r']:,}")
        print(f"observed : {row['lo']} -> {row['hi']}")

        print("\nVersions per record (how much history exists):")
        for r in conn.execute(
            "SELECT version_number, COUNT(*) n FROM raw_record WHERE is_current = 1"
            " GROUP BY version_number ORDER BY version_number"
        ):
            print(f"  v{r['version_number']:<3} {r['n']:>8,} records")

        print("\nBy source system:")
        print(f"  {'source':<22}{'records':>10}{'versions':>10}{'multi-ver':>11}")
        for r in conn.execute(
            """
            SELECT source_system,
                   COUNT(DISTINCT source_record_id) records,
                   COUNT(*) versions,
                   SUM(CASE WHEN is_current = 1 AND version_number > 1 THEN 1 ELSE 0 END) multi
              FROM raw_record GROUP BY source_system ORDER BY versions DESC
            """
        ):
            print(f"  {r['source_system']:<22}{r['records']:>10,}"
                  f"{r['versions']:>10,}{r['multi']:>11,}")

        print("\nRecent batches:")
        print(f"  {'id':>5} {'source':<16}{'type':<11}{'fetch':>7}{'new':>7}"
              f"{'chg':>7}{'same':>7}  status")
        for r in conn.execute(
            "SELECT batch_id, source_system, connector_type, records_fetched, records_new,"
            " records_changed, records_unchanged, status FROM raw_ingest_batch"
            " ORDER BY batch_id DESC LIMIT 15"
        ):
            print(f"  {r['batch_id']:>5} {r['source_system']:<16}"
                  f"{(r['connector_type'] or '-'):<11}{r['records_fetched']:>7,}"
                  f"{r['records_new']:>7,}{r['records_changed']:>7,}"
                  f"{r['records_unchanged']:>7,}  {r['status']}")

        print("\nMost-versioned records (churn suspects):")
        for r in conn.execute(
            "SELECT source_system, source_record_id, version_number FROM raw_record"
            " WHERE is_current = 1 ORDER BY version_number DESC LIMIT 10"
        ):
            print(f"  {r['source_system']:<16}{r['source_record_id']:<28}"
                  f"v{r['version_number']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
