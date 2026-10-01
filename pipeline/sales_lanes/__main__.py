"""Phase 4F CLI.

    python -m pipeline.sales_lanes

Assigns internal sales lanes, collapses canonical duplicates for presentation,
and writes lane books + an internal HTML prototype. Does not change scores.
"""

from __future__ import annotations

import time

from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE
from pipeline.db.database import init_db
from pipeline.entity.report import score_guardrails
from pipeline.relevance.account_report import top_accounts
from pipeline.sales_lanes.book import build_lane_book, persist_books
from pipeline.sales_lanes.collapse import collapse_stats
from pipeline.sales_lanes.lanes import FIRE_BACKFLOW, FUEL_GAS_PROPANE, CIVIL_WET_UTILITY, GENERAL_CONTRACTOR_CM, PLUMBING_CORE
from pipeline.sales_lanes.report import write_phase4f_report
from pipeline.sales_lanes.store import assign_all
from pipeline.sales_lanes.view import write_internal_view


def run(conn) -> dict:
    before = score_guardrails(conn)
    t0 = time.time()
    assign_stats = assign_all(conn)
    top100 = top_accounts(conn, limit=100)
    names = {int(r["company_id"]): r["display_name"] for r in top100}
    collapse = collapse_stats(conn, [int(r["company_id"]) for r in top100], names)
    books = {
        PLUMBING_CORE: build_lane_book(conn, PLUMBING_CORE, limit=25),
        FUEL_GAS_PROPANE: build_lane_book(conn, FUEL_GAS_PROPANE, limit=10),
        FIRE_BACKFLOW: build_lane_book(conn, FIRE_BACKFLOW, limit=10),
        CIVIL_WET_UTILITY: build_lane_book(conn, CIVIL_WET_UTILITY, limit=10),
        GENERAL_CONTRACTOR_CM: build_lane_book(conn, GENERAL_CONTRACTOR_CM, limit=10),
    }
    persist_books(conn, books)
    html_path = write_internal_view(books)
    elapsed = round(time.time() - t0, 1)
    after = score_guardrails(conn)
    if before != after:
        raise RuntimeError(f"Phase 4F mutated ranking/CRM guardrails: {before} -> {after}")
    path = write_phase4f_report(
        conn,
        books=books,
        assign_stats=assign_stats,
        collapse=collapse,
        html_path=html_path,
        elapsed_s=elapsed,
        before_guard=before,
        after_guard=after,
    )
    return {
        "elapsed_s": elapsed,
        "assign": assign_stats,
        "collapse_top100": collapse,
        "plumbing_core_n": len(books[PLUMBING_CORE]),
        "report": str(path),
        "html": str(html_path),
        "guardrails": after,
        "profile": CUSTOMER_RELEVANCE_PROFILE,
    }


def main() -> int:
    conn = init_db()
    try:
        result = run(conn)
        print("phase4f", result)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
