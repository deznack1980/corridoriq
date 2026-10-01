"""CorridorIQ pipeline CLI.

    python pipeline/run.py                     full chain
    python pipeline/run.py ingest
    python pipeline/run.py analyze
    python pipeline/run.py rebuild-contractors
    python pipeline/run.py companies
    python pipeline/run.py export
    python pipeline/run.py export-permits
    python pipeline/run.py reports
    python -m pipeline.run morning_refresh     automated daily refresh

init-db runs automatically first every time (idempotent) — doubles as the
migration mechanism for jurisdictions.yaml config changes. No scheduler/cron
wiring here; run manually or wire up an external scheduler later.
"""

import argparse
import sys
import time
from pathlib import Path

# Allow `python pipeline/run.py` (direct script execution) in addition to
# `python -m pipeline.run` — both need the project root on sys.path so the
# `pipeline` package resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.analysis.run_analysis import run_analysis
from pipeline.contractors.rebuild import rebuild_contractors
from pipeline.db.database import init_db
from pipeline.export.export_json import export_all, export_all_permits_csv
from pipeline.ingestion.backfill_identity import backfill_identity_fields
from pipeline.ingestion.run_ingestion import run_ingestion
from pipeline.reports.generate_reports import generate_all_reports


def cmd_ingest(conn):
    print("=== Ingestion ===")
    run_ingestion(conn)


def cmd_analyze(conn):
    print("=== Analysis ===")
    n = run_analysis(conn)
    print(f"Analyzed {n} permits.")


def cmd_rebuild_contractors(conn):
    print("=== Contractor rebuild ===")
    n = rebuild_contractors(conn)
    print(f"Rebuilt {n} contractor profiles.")


def cmd_companies(conn):
    """Company Intelligence: link permits/projects to canonical companies,
    compute metrics + timeline, and export the UI feed. Idempotent/resumable;
    never runs permit scoring."""
    from pipeline.company_resolution.backfill import backfill_companies
    from pipeline.company_resolution.export import export_all as export_companies
    from pipeline.company_resolution.metrics import compute_company_metrics
    from pipeline.company_resolution.timeline import rebuild_company_timeline

    print("=== Company identity backfill ===")
    stats = backfill_companies(conn, resume=True)
    print(f"  {stats.as_dict()}")
    print("=== Company metrics ===")
    n = compute_company_metrics(conn)
    print(f"  metrics for {n} companies")
    print("=== Company timeline ===")
    t = rebuild_company_timeline(conn)
    print(f"  {t} new timeline rows")
    print("=== Contractor capability classification (internal) ===")
    from pipeline.contractor_intel.classify import classify_companies

    cap = classify_companies(conn)
    print(f"  {cap}")
    print("=== Customer relevance (shadow, plumbing_supply) ===")
    from pipeline.relevance.classify import score_project_relevance

    rel = score_project_relevance(conn)
    print(f"  {rel}")
    print("=== Account priority (shadow, plumbing_supply) ===")
    from pipeline.relevance.account import score_company_accounts

    acc = score_company_accounts(conn)
    print(f"  {acc}")
    export_companies(conn)
    print("  company exports written")


def cmd_export(conn):
    print("=== JSON export ===")
    export_all(conn)
    print("Export complete.")


def cmd_export_permits(conn):
    print("=== Full permit CSV archive ===")
    paths = export_all_permits_csv(conn)
    for path in paths:
        print(f"Wrote: {path}")


def cmd_reports(conn):
    print("=== Identity backfill (from stored raw fields) ===")
    counts = backfill_identity_fields(conn)
    for rule, n in counts.items():
        print(f"  {rule}: {n} rows updated")
    print("=== Report generation ===")
    for path in generate_all_reports(conn):
        print(f"Generated: {path}")
    print("=== Company intelligence reports ===")
    from pipeline.company_resolution.reports import generate_company_reports

    for key, path in generate_company_reports(conn).items():
        print(f"Generated ({key}): {path}")
    print("=== Security & CRM access validation reports ===")
    from pipeline.reports.security_validation import generate_all as generate_security_reports

    for path in generate_security_reports():
        print(f"Generated: {path}")

    print("=== Supplier catalog + price comparison reports ===")
    from pipeline.products.reports import generate_reports as generate_product_reports

    for path in generate_product_reports(conn):
        print(f"Generated: {path}")


def cmd_morning_refresh(conn):
    """Automated morning refresh: full chain, per-jurisdiction failure
    isolation, incremental scoring, and a recorded run summary. Same code path
    for the scheduler and a manual admin trigger."""
    from pipeline.pipeline_runs import RunInProgressError, run_morning_refresh

    print("=== Morning refresh ===")
    try:
        summary = run_morning_refresh(conn, trigger_source="cli", verbose=True)
    except RunInProgressError as exc:
        print(f"SKIPPED — {exc}")
        return
    print(
        f"status={summary['status']} "
        f"jurisdictions={summary['jurisdictions_succeeded']}/{summary['jurisdictions_attempted']} "
        f"new_submitted={summary['new_submitted_opportunities']} "
        f"new_issued={summary['new_issued_permits']} "
        f"updated={summary['updated_projects']} in {summary['duration_seconds']}s"
    )


def cmd_raw_backfill(conn):
    """One-time baseline snapshot of existing permits into the RAW layer."""
    from pipeline.ingestion.raw_backfill import backfill_permits

    print("\n=== RAW baseline backfill ===")
    stats = backfill_permits(conn)
    print(
        f"scanned={stats['scanned']} new={stats['new']} changed={stats['changed']} "
        f"unchanged={stats['unchanged']} skipped={stats['skipped']}"
    )


def cmd_source_health(conn):
    """Evaluate and record per-source health telemetry."""
    from pipeline.telemetry import source_health

    print("\n=== Source health ===")
    for health in source_health.record_snapshot(conn):
        print(f"{health['jurisdiction_slug']:<16}{health['health_state']:<10}"
              f"{health['detail'] or ''}")


def cmd_classify_contractors(conn):
    """Internal multi-label contractor capabilities. Does not change scores."""
    from pipeline.contractor_intel.classify import classify_companies

    print("=== Contractor capability classification (internal) ===")
    stats = classify_companies(conn)
    print(f"  {stats}")


def cmd_shadow_relevance(conn):
    """Shadow plumbing_supply relevance. Does not change opportunity_score."""
    from pipeline.relevance.classify import score_project_relevance

    print("=== Customer relevance (shadow, plumbing_supply) ===")
    stats = score_project_relevance(conn)
    print(f"  {stats}")


def cmd_shadow_accounts(conn):
    """Shadow account priority. Does not rewrite project relevance scores."""
    from pipeline.relevance.account import score_company_accounts

    print("=== Account priority (shadow, plumbing_supply) ===")
    stats = score_company_accounts(conn)
    print(f"  {stats}")


STEPS = {
    "ingest": cmd_ingest,
    "raw_backfill": cmd_raw_backfill,
    "source_health": cmd_source_health,
    "analyze": cmd_analyze,
    "rebuild-contractors": cmd_rebuild_contractors,
    "companies": cmd_companies,
    "export": cmd_export,
    "export-permits": cmd_export_permits,
    "reports": cmd_reports,
    "morning_refresh": cmd_morning_refresh,
    "classify-contractors": cmd_classify_contractors,
    "shadow-relevance": cmd_shadow_relevance,
    "shadow-accounts": cmd_shadow_accounts,
}


def main():
    parser = argparse.ArgumentParser(description="CorridorIQ data pipeline")
    parser.add_argument(
        "step",
        nargs="?",
        choices=list(STEPS.keys()),
        default=None,
        help="Run a single step. Omit to run the full chain.",
    )
    args = parser.parse_args()

    start = time.time()
    conn = init_db()

    try:
        if args.step:
            STEPS[args.step](conn)
        else:
            for step_fn in (
                cmd_ingest,
                cmd_analyze,
                cmd_rebuild_contractors,
                cmd_companies,
                cmd_export,
                cmd_reports,
            ):
                step_fn(conn)
    finally:
        conn.close()

    elapsed = time.time() - start
    print(f"\nDone in {elapsed:.1f}s.")


if __name__ == "__main__":
    sys.exit(main())
