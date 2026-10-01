"""One-off deep catch-up for records the old watermark permanently skipped.

Background: until 2026-09-15 the incremental watermark was `last_synced_at` --
when the pipeline last ran -- while connectors filter on the source's *issue
date*. Records published after their issue date fell behind the advancing
watermark and were never collected. `scripts/check_source_gaps.py` measured at
least 669 such permits across five sources.

The standing fix (`compute_since`) watermarks on the newest source date held
minus 30 days, which recovers recent losses on its own. This script widens that
window once so older losses are recovered too.

    python scripts/catchup_ingest.py --dry-run
    python scripts/catchup_ingest.py --lookback-days 400

Safe to re-run: records that have not changed are detected as unchanged and do
not bump `last_updated_at`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.db.database import get_connection  # noqa: E402
from pipeline.ingestion.run_ingestion import compute_since, run_ingestion  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-days", type=int, default=400,
                        help="how far before the newest held record to re-query")
    parser.add_argument("--dry-run", action="store_true",
                        help="show the watermark each source would use, fetch nothing")
    args = parser.parse_args()

    conn = get_connection()
    try:
        sources = list(conn.execute(
            "SELECT slug, connector_type, last_synced_at FROM jurisdictions "
            "WHERE status='connected' ORDER BY slug"))

        print(f"Catch-up lookback: {args.lookback_days} days")
        print(f"  {'source':<16}{'old watermark (last run)':<28}{'new watermark'}")
        for row in sources:
            new = compute_since(conn, row["slug"], row["last_synced_at"],
                                args.lookback_days)
            old = str(row["last_synced_at"] or "-")[:19]
            print(f"  {row['slug']:<16}{old:<28}{new.date().isoformat()}")

        if args.dry_run:
            print("\nDry run - nothing fetched.")
            return 0

        print()
        run_ingestion(conn, lookback_days=args.lookback_days)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
