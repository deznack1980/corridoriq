"""Source health: roll ingestion_runs up into a per-jurisdiction verdict.

Data platform Phase 2. The pipeline already recorded every run, but answering
"is this source reliable?" meant writing SQL by hand. That is how gilbert_az
managed to fail on 2026-09-08, 09-13 and 09-15 before the pattern was noticed:
each individual run looked like an isolated blip, and nothing aggregated them.

States, in precedence order:

    failing  - a sustained failure streak (>= SOURCE_FAILING_STREAK)
    degraded - an isolated failure, or repeated failures within the window
    silent   - runs succeed but return nothing, for a source that used to
               return something
    healthy  - recent runs succeeding
    unknown  - no runs recorded yet

`silent` is the state worth having. A withdrawn feed, a renamed field, or a
broken incremental filter all produce successful HTTP 200s with zero rows,
which is indistinguishable from "no new permits were issued" until enough
quiet time has passed.

Deliberately NOT a health input: how old the newest permit is. A city that is
not issuing permits is not a broken source, and conflating the two produces
alerts nobody trusts. It is reported alongside the state instead.

    python -m pipeline.telemetry.source_health
    python -m pipeline.telemetry.source_health --history gilbert_az
    python -m pipeline.telemetry.source_health --json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone

from pipeline.config import settings

HEALTHY = "healthy"
DEGRADED = "degraded"
FAILING = "failing"
SILENT = "silent"
UNKNOWN = "unknown"

# Worst first, for sorting a report so problems are at the top.
STATE_ORDER = {FAILING: 0, SILENT: 1, DEGRADED: 2, UNKNOWN: 3, HEALTHY: 4}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def _days_since(value, now: datetime) -> float | None:
    moment = _parse(value)
    if moment is None:
        return None
    return round((now - moment).total_seconds() / 86400, 2)


def _is_success(status) -> bool:
    return bool(status) and str(status).startswith("success")


def evaluate(conn: sqlite3.Connection, slug: str, now: datetime | None = None) -> dict:
    """Compute the current health of one jurisdiction."""
    now = now or _now()
    runs = list(conn.execute(
        "SELECT run_started_at, status, records_fetched, error_type, error_message "
        "FROM ingestion_runs WHERE jurisdiction_slug = ? "
        "ORDER BY run_started_at DESC, id DESC",
        (slug,),
    ))

    health = {
        "jurisdiction_slug": slug,
        "health_state": UNKNOWN,
        "consecutive_failures": 0,
        "last_success_at": None,
        "last_data_at": None,
        "last_error_type": None,
        "last_error_message": None,
        "runs_7d": 0,
        "failures_7d": 0,
        "success_rate_7d": None,
        "records_7d": 0,
        "newest_source_date": None,
        "days_since_newest_source": None,
        "days_since_last_data": None,
        "detail": None,
    }

    newest = conn.execute(
        "SELECT MAX(COALESCE(issued_date, filed_date, finaled_date)) AS d "
        "FROM permits WHERE jurisdiction = ?",
        (slug,),
    ).fetchone()
    if newest and newest["d"]:
        health["newest_source_date"] = newest["d"]
        health["days_since_newest_source"] = _days_since(newest["d"], now)

    if not runs:
        health["detail"] = "no ingestion runs recorded"
        return health

    streak = 0
    for run in runs:
        if _is_success(run["status"]):
            break
        streak += 1
    health["consecutive_failures"] = streak

    for run in runs:
        if health["last_success_at"] is None and _is_success(run["status"]):
            health["last_success_at"] = run["run_started_at"]
        if health["last_data_at"] is None and (run["records_fetched"] or 0) > 0:
            health["last_data_at"] = run["run_started_at"]
        if health["last_error_type"] is None and not _is_success(run["status"]):
            health["last_error_type"] = run["error_type"]
            health["last_error_message"] = run["error_message"]
        if all(health[k] is not None for k in
               ("last_success_at", "last_data_at", "last_error_type")):
            break

    window_start = now.timestamp() - settings.SOURCE_HEALTH_WINDOW_DAYS * 86400
    in_window = [r for r in runs
                 if (_parse(r["run_started_at"]) or now).timestamp() >= window_start]
    health["runs_7d"] = len(in_window)
    health["failures_7d"] = sum(1 for r in in_window if not _is_success(r["status"]))
    health["records_7d"] = sum((r["records_fetched"] or 0) for r in in_window)
    if in_window:
        health["success_rate_7d"] = round(
            (len(in_window) - health["failures_7d"]) / len(in_window), 3
        )

    health["days_since_last_data"] = _days_since(health["last_data_at"], now)

    health["health_state"], health["detail"] = _classify(health, runs, now)
    return health


def _classify(health: dict, runs: list, now: datetime) -> tuple[str, str]:
    streak = health["consecutive_failures"]
    if streak >= settings.SOURCE_FAILING_STREAK:
        kind = health["last_error_type"] or "error"
        return FAILING, f"{streak} consecutive failed runs ({kind})"
    if streak >= 1:
        kind = health["last_error_type"] or "error"
        return DEGRADED, f"last run failed ({kind})"
    if health["failures_7d"] >= settings.SOURCE_DEGRADED_FAILURES_7D:
        return DEGRADED, (f"{health['failures_7d']} failures in the last "
                          f"{settings.SOURCE_HEALTH_WINDOW_DAYS} days")

    since_data = health["days_since_last_data"]
    if since_data is not None:
        if since_data >= settings.SOURCE_SILENT_DAYS:
            return SILENT, (f"succeeding but no records returned for "
                            f"{since_data:.0f} days")
    else:
        # Never returned a single record. Only meaningful once the source has
        # had a fair chance; a freshly connected jurisdiction is not silent.
        first = _parse(runs[-1]["run_started_at"])
        age = (now - first).total_seconds() / 86400 if first else 0
        if age >= settings.SOURCE_SILENT_DAYS:
            return SILENT, f"has never returned a record in {age:.0f} days"
        return HEALTHY, "recently connected, no data yet"

    return HEALTHY, f"{health['records_7d']} records in the last "\
                    f"{settings.SOURCE_HEALTH_WINDOW_DAYS} days"


def evaluate_all(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Health for every connected jurisdiction, worst first."""
    now = now or _now()
    slugs = [r["slug"] for r in conn.execute(
        "SELECT slug FROM jurisdictions WHERE status = 'connected' ORDER BY slug"
    )]
    results = [evaluate(conn, slug, now) for slug in slugs]
    results.sort(key=lambda h: (STATE_ORDER.get(h["health_state"], 9),
                                h["jurisdiction_slug"]))
    return results


def record_snapshot(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Evaluate every source and append the verdicts to source_health_snapshot.

    Stored rather than only computed so reliability becomes a trend. Returns
    the evaluations so a caller can log or display them.
    """
    now = now or _now()
    results = evaluate_all(conn, now)
    captured_at = now.isoformat(timespec="seconds")
    for health in results:
        conn.execute(
            """
            INSERT INTO source_health_snapshot (
                captured_at, jurisdiction_slug, health_state, consecutive_failures,
                last_success_at, last_data_at, last_error_type, last_error_message,
                runs_7d, failures_7d, success_rate_7d, records_7d,
                newest_source_date, days_since_newest_source, days_since_last_data,
                detail
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (captured_at, health["jurisdiction_slug"], health["health_state"],
             health["consecutive_failures"], health["last_success_at"],
             health["last_data_at"], health["last_error_type"],
             (health["last_error_message"] or "")[:500] or None,
             health["runs_7d"], health["failures_7d"], health["success_rate_7d"],
             health["records_7d"], health["newest_source_date"],
             health["days_since_newest_source"], health["days_since_last_data"],
             health["detail"]),
        )
    conn.commit()
    return results


def record_snapshot_safe(conn: sqlite3.Connection) -> list[dict]:
    """record_snapshot that can never break the caller.

    Telemetry is an observer. An ingestion run must not fail because the
    reporting layer could not write a row.
    """
    try:
        return record_snapshot(conn)
    except Exception as exc:  # noqa: BLE001
        print(f"Source health snapshot skipped: {exc}")
        return []


def history(conn: sqlite3.Connection, slug: str, limit: int = 30) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM source_health_snapshot WHERE jurisdiction_slug = ? "
        "ORDER BY captured_at DESC, id DESC LIMIT ?",
        (slug, int(limit)),
    )]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_day(value) -> str:
    return str(value)[:10] if value else "-"


def _print_table(results: list[dict]) -> None:
    print(f"  {'source':<16}{'state':<10}{'strk':>5}{'runs7':>6}{'fail7':>6}"
          f"{'rec7':>7}  {'last ok':<11}{'last data':<11}{'newest':<11} detail")
    for h in results:
        print(f"  {h['jurisdiction_slug']:<16}{h['health_state']:<10}"
              f"{h['consecutive_failures']:>5}{h['runs_7d']:>6}{h['failures_7d']:>6}"
              f"{h['records_7d']:>7}  "
              f"{_fmt_day(h['last_success_at']):<11}"
              f"{_fmt_day(h['last_data_at']):<11}"
              f"{_fmt_day(h['newest_source_date']):<11} {h['detail'] or ''}")


def main() -> int:
    from pipeline.db.database import get_connection

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--history", metavar="SLUG",
                        help="show recorded health snapshots for one source")
    parser.add_argument("--persist", action="store_true",
                        help="record a snapshot as well as printing")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.history:
            rows = history(conn, args.history)
            if args.json:
                print(json.dumps(rows, indent=2, default=str))
                return 0
            if not rows:
                print(f"No health snapshots recorded for {args.history}.")
                return 0
            print(f"Health history for {args.history} (newest first):")
            print(f"  {'captured':<21}{'state':<10}{'strk':>5}{'fail7':>6}  detail")
            for r in rows:
                print(f"  {str(r['captured_at'])[:19]:<21}{r['health_state']:<10}"
                      f"{r['consecutive_failures']:>5}{r['failures_7d']:>6}  "
                      f"{r['detail'] or ''}")
            return 0

        results = record_snapshot(conn) if args.persist else evaluate_all(conn)
        if args.json:
            print(json.dumps(results, indent=2, default=str))
            return 0

        print("=" * 78)
        print(" CorridorIQ - source health")
        print("=" * 78)
        _print_table(results)

        counts: dict[str, int] = {}
        for h in results:
            counts[h["health_state"]] = counts.get(h["health_state"], 0) + 1
        print("\n  " + "  ".join(f"{state}={n}" for state, n in sorted(counts.items())))
        if args.persist:
            print(f"  snapshot recorded for {len(results)} sources")

        unhealthy = [h for h in results if h["health_state"] in (FAILING, SILENT)]
        return 1 if unhealthy else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
