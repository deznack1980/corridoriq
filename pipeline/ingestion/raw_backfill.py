"""One-time baseline snapshot of existing permits into the RAW layer.

Data platform Phase 1. Before RAW capture existed, the only copy of a source
payload was ``permits.raw_source_json``, which was overwritten on every update.
This module seeds one RAW version per existing permit from that column so the
new layer starts from a complete picture of the corpus rather than from zero.

HONESTY NOTE: these rows are a snapshot, not observed history. They are written
into a batch whose ``connector_type`` is ``'backfill'`` and whose source_system
is the real jurisdiction, and ``fetched_at`` is set to the permit's
``first_seen_at`` (when we first saw the permit) rather than to now. Any
analysis of change-over-time must treat version 1 of a backfilled record as a
starting point, not as evidence that nothing changed before it.

Idempotent: re-running captures nothing, because every payload hash already
matches the current version.

    python -m pipeline.run raw_backfill
    python -m pipeline.ingestion.raw_backfill --limit 1000
"""

from __future__ import annotations

import argparse
import sqlite3

from pipeline.config import settings
from pipeline.ingestion import raw_capture

BATCH_COMMIT_SIZE = 2_000


def backfill_permits(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    progress=None,
) -> dict:
    """Seed RAW with one version per existing permit. Returns a stats dict."""
    total = conn.execute("SELECT COUNT(*) FROM permits").fetchone()[0]
    if limit:
        total = min(total, limit)

    batch_id = raw_capture.open_batch(
        conn,
        source_system=settings.RAW_BACKFILL_SOURCE,
        source_entity_type=settings.RAW_ENTITY_PERMIT,
        connector_type="backfill",
        source_url=None,
        requested_since=None,
    )

    sql = (
        "SELECT jurisdiction, permit_number, raw_source_json, permit_url, "
        "       first_seen_at, last_updated_at "
        "FROM permits ORDER BY id"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"

    stats = {"scanned": 0, "new": 0, "changed": 0, "unchanged": 0, "skipped": 0,
             "batch_id": batch_id}

    # A separate cursor so the streaming read is not disturbed by the writes.
    read_cursor = conn.execute(sql)
    for row in read_cursor:
        stats["scanned"] += 1
        payload = row["raw_source_json"]
        if not payload or payload == "{}":
            stats["skipped"] += 1
            continue

        outcome = raw_capture.capture(
            conn,
            batch_id,
            source_system=row["jurisdiction"],
            source_record_id=row["permit_number"],
            payload=payload,
            source_entity_type=settings.RAW_ENTITY_PERMIT,
            source_url=row["permit_url"],
            # When we first observed the permit, not when we ran the backfill.
            fetched_at=row["first_seen_at"],
        )
        stats[outcome] += 1

        if stats["scanned"] % BATCH_COMMIT_SIZE == 0:
            conn.commit()
            if progress:
                progress(stats["scanned"], total)

    conn.commit()
    if progress:
        progress(stats["scanned"], total)

    raw_capture.close_batch(
        conn, batch_id, status="succeeded",
        fetched=stats["scanned"], new=stats["new"],
        changed=stats["changed"], unchanged=stats["unchanged"],
    )
    return stats


def _print_progress(done: int, total: int) -> None:
    pct = (done / total * 100) if total else 100.0
    print(f"  {done:,} / {total:,} ({pct:.1f}%)", flush=True)


def main() -> int:
    from pipeline.db.database import get_connection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    try:
        print("Seeding RAW baseline snapshot from existing permits ...")
        stats = backfill_permits(
            conn, limit=args.limit,
            progress=None if args.quiet else _print_progress,
        )
        print("\nBaseline snapshot complete:")
        print(f"  batch_id  : {stats['batch_id']}")
        print(f"  scanned   : {stats['scanned']:,}")
        print(f"  new       : {stats['new']:,}")
        print(f"  changed   : {stats['changed']:,}")
        print(f"  unchanged : {stats['unchanged']:,}  (already captured)")
        print(f"  skipped   : {stats['skipped']:,}  (no raw payload stored)")
        totals = raw_capture.stats(conn)
        print(f"\nRAW layer now holds {totals.get('versions', 0):,} versions "
              f"across {totals.get('records', 0):,} records.")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
