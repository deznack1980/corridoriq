"""Shadow plumbing-supply relevance and account priority.

    python -m pipeline.relevance
    python -m pipeline.relevance --accounts --backtest
"""

from __future__ import annotations

import argparse
import time

from pipeline.config.settings import (
    ACCOUNT_PRIORITY_MODEL_VERSION,
    CUSTOMER_RELEVANCE_MODEL_VERSION,
)
from pipeline.db.database import init_db
from pipeline.relevance.account import score_company_accounts
from pipeline.relevance.account_report import known_rows, top_accounts, write_account_report
from pipeline.relevance.classify import score_project_relevance
from pipeline.relevance.report import (
    named_company_stats,
    summarize,
    top_rows,
    write_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", action="store_true")
    parser.add_argument(
        "--accounts",
        action="store_true",
        help="score shadow account priority; does not rebuild project relevance",
    )
    args = parser.parse_args()

    conn = init_db()
    try:
        if args.accounts:
            t0 = time.time()
            stats = score_company_accounts(conn)
            elapsed = time.time() - t0
            print(
                f"model={stats['model_version']} accounts={stats['accounts']} "
                f"labeled={stats['labeled_projects']} "
                f"relevance_writes={stats['relevance_rows_written']} "
                f"in {elapsed:.1f}s"
            )
            if args.backtest:
                top = top_accounts(conn, limit=100)
                known = known_rows(conn)
                path = write_account_report(conn, stats=stats, top=top, known=known)
                print("demand", stats.get("demand_histogram"))
                print("top5", [(r["rank"], r["display_name"], r["account_priority_score"], r["trade_identity"]) for r in top[:5]])
                print(f"report={path}")
            print(ACCOUNT_PRIORITY_MODEL_VERSION)
            return 0

        t0 = time.time()
        stats = score_project_relevance(conn)
        elapsed = time.time() - t0
        print(
            f"model={stats['model_version']} profile={stats['profile_key']} "
            f"rows={stats['rows']} opportunity_writes={stats['opportunity_rows_written']} "
            f"in {elapsed:.1f}s"
        )
        if args.backtest:
            summary = summarize(conn)
            summary["runtime_s"] = round(elapsed, 1)
            named = named_company_stats(
                conn,
                ["ToyotaLift", "TOYOTALIFT", "Kerns", "Shea Homes", "Pulte"],
            )
            path = write_report(
                conn, summary, named=named,
                top_relevance=top_rows(conn, order="relevance"),
                top_opportunity=top_rows(conn, order="opportunity"),
            )
            print(
                f"high/med/border={summary['high']}/{summary['medium']}/{summary['borderline']} "
                f"avg={summary['avg_score']} incidental_high={summary['incidental_high']}"
            )
            print("named", named)
            print(f"report={path}")
        print(CUSTOMER_RELEVANCE_MODEL_VERSION)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
