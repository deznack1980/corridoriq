"""Backfill Phoenix permit latitude/longitude from the city's point layer.

Dry run by default. --apply refuses while a pipeline run is active, takes and
verifies a LOCAL backup first (no off-site replication), then fills only rows
whose coordinates are empty, in one transaction. A JSON report is written to
reports/generated/ either way.

    py -3 scripts/backfill_phoenix_coordinates.py            # dry run
    py -3 scripts/backfill_phoenix_coordinates.py --apply    # write
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import requests  # noqa: E402

from pipeline.config.settings import DB_PATH, HTTP_REQUEST_DELAY_SECONDS, HTTP_USER_AGENT, REPORTS_GENERATED_DIR  # noqa: E402
from pipeline.ingestion import phoenix_coordinates as pc  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write coordinates (default: dry run)")
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({"User-Agent": HTTP_USER_AGENT})
    print("Reading Phoenix point layer (PER_NUM + WGS84 geometry)...")
    points = list(pc.source_points(pc.http_fetch_page(session, HTTP_REQUEST_DELAY_SECONDS)))

    ro = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)
    result = pc.plan(ro, points)
    running = ro.execute("SELECT COUNT(*) FROM pipeline_runs WHERE status = 'running'").fetchone()[0]
    ro.close()
    updates = result.pop("updates")
    print(json.dumps(result, indent=2))

    report = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "mode": "apply" if args.apply else "dry_run", **result}
    if args.apply:
        if running:
            print("A pipeline run is in progress; not writing.")
            return 2
        if not updates:
            print("Nothing to fill.")
        else:
            from pipeline.db.backup import create_backup, verify

            target = create_backup()
            ok, detail, rows = verify(target)
            print(f"Local backup {target.name}: verified={ok} ({detail}), {rows:,} rows")
            if not ok:
                print("Backup did not verify; not writing.")
                return 3
            rw = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
            rw.execute("PRAGMA busy_timeout = 30000")
            written = pc.apply(rw, updates)
            filled = rw.execute(
                "SELECT COUNT(*) FROM permits WHERE jurisdiction='phoenix_az' AND latitude IS NOT NULL"
            ).fetchone()[0]
            rw.close()
            report.update({"backup": target.name, "rows_written": written, "phoenix_with_coordinates_after": filled})
            print(f"Wrote {written:,} rows. Phoenix permits with coordinates: {filled:,}")
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_GENERATED_DIR / f"phoenix_coordinates_backfill_{stamp}.json"
    path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"Report: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
