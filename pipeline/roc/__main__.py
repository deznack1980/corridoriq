"""Arizona ROC identity enrichment CLI.

    python -m pipeline.roc --csv PATH --validate --backtest

Does not change dashboard ranking or account_priority_score.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from pipeline.config.settings import ROC_DATA_DIR, ROC_MODEL_VERSION
from pipeline.db.database import init_db
from pipeline.roc.ingest import ingest_posting_list
from pipeline.roc.match import match_companies
from pipeline.roc.report import write_roc_report
from pipeline.roc.simulate import simulate_account_priority
from pipeline.roc.validate import validate_identities
from pipeline.relevance.identity import reserved_license_payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", type=Path, help="Official ROC posting-list CSV")
    parser.add_argument("--validate", action="store_true")
    parser.add_argument("--backtest", action="store_true")
    args = parser.parse_args()

    csv_path = args.csv
    if csv_path is None:
        latest = sorted(ROC_DATA_DIR.glob("ROC_Posting-List_*.csv")) if ROC_DATA_DIR.exists() else []
        csv_path = latest[-1] if latest else None
    if csv_path is None or not csv_path.exists():
        print("No ROC posting-list CSV. Download All Current Contractors from https://roc.az.gov/posting-list")
        return 2

    conn = init_db()
    t0 = time.time()
    try:
        ingest_stats = ingest_posting_list(conn, csv_path)
        match_stats = match_companies(conn)
        validate_stats = {"skipped": True}
        simulation = {"skipped": True}
        if args.validate:
            validate_stats = validate_identities(conn)
            simulation = simulate_account_priority(conn)
        elapsed = time.time() - t0
        roc_on = conn.execute(
            "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='roc'"
        ).fetchone()
        sample = conn.execute("SELECT id FROM companies LIMIT 1").fetchone()
        payload = reserved_license_payload(conn, int(sample["id"])) if sample else None
        print(
            f"model={ROC_MODEL_VERSION} rows={ingest_stats['rows']} "
            f"roc_enabled={ingest_stats['roc_enabled']} "
            f"payload_if_disabled={payload is not None} "
            f"in {elapsed:.1f}s"
        )
        print("ingest", ingest_stats)
        print("match", match_stats)
        print("validate", validate_stats)
        if args.backtest:
            path = write_roc_report(
                conn,
                ingest_stats=ingest_stats,
                match_stats=match_stats,
                validate_stats=validate_stats,
                simulation=simulation,
                elapsed_s=round(elapsed, 1),
            )
            print(f"report={path}")
        print("ranking_flag", int(roc_on[0] if roc_on else 0))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
