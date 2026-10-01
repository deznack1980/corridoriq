"""Classify contractor capabilities (internal). Does not change opportunity scores.

    python -m pipeline.contractor_intel
    python -m pipeline.contractor_intel --backtest
"""

from __future__ import annotations

import argparse
import time

from pipeline.config.settings import CONTRACTOR_INTEL_MODEL_VERSION
from pipeline.contractor_intel.classify import classify_companies
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.contractor_intel.report import (
    example_rows,
    false_positive_candidates,
    summarize,
    write_report,
)
from pipeline.contractor_intel.review import (
    build_review_sample,
    summarize_judgments,
    write_review_sample,
)
from pipeline.db.database import init_db


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backtest", action="store_true",
                        help="classify then print/write aggregate review stats")
    args = parser.parse_args()

    conn = init_db()
    try:
        seed_enrichment_registry(conn)
        t0 = time.time()
        stats = classify_companies(conn)
        elapsed = time.time() - t0
        print(f"model={stats['model_version']} companies={stats['companies_scanned']} "
              f"capability_rows={stats['capability_rows']} evidence={stats['evidence_rows']} "
              f"in {elapsed:.1f}s")
        if args.backtest:
            summary = summarize(conn)
            summary["runtime_s"] = round(elapsed, 1)
            summary["evidence_rows"] = stats["evidence_rows"]
            high = example_rows(conn, "fuel_gas", min_conf=75, max_conf=101, limit=5)
            plumbing_high = example_rows(conn, "plumbing", min_conf=75, max_conf=101, limit=5)
            borderline = example_rows(conn, "fuel_gas", min_conf=20, max_conf=50, limit=5)
            fps = false_positive_candidates(conn)
            path = write_report(conn, summary, high=high, borderline=borderline, fps=fps)
            samples = build_review_sample(conn)
            review_path = write_review_sample(samples)
            fg, pl = summary["fuel_gas"], summary["plumbing"]
            print(f"plumbing={pl['total']} fuel_gas={fg['total']} both={summary['both_companies']}")
            print(f"fuel_gas high/med/border={fg['high']}/{fg['medium']}/{fg['borderline']} "
                  f"high_gc={fg['high_gc']} specialists={fg['specialists']}")
            print(f"plumbing high/med/border={pl['high']}/{pl['medium']}/{pl['borderline']} "
                  f"high_gc={pl['high_gc']} specialists={pl['specialists']}")
            print(f"gas_permits={summary['gas_evidence_permits']} "
                  f"useful={summary['useful_pct']}% any={summary['useful_any_pct']}%")
            print(f"runtime={elapsed:.1f}s evidence={stats['evidence_rows']}")
            print(f"report={path}")
            print(f"review={review_path}")
            print("review_judgments", summarize_judgments(samples))
            if plumbing_high:
                print("plumbing_high", [r["display_name"] for r in plumbing_high])
            print("named", {k: v for k, v in summary["named"].items()})
        print(CONTRACTOR_INTEL_MODEL_VERSION)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
