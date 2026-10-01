"""Phase 4G CLI.

    python -m pipeline.sales_trial

Freezes the current PLUMBING_CORE Top 25, adds public-business contacts,
and writes an internal call sheet. Does not change scores or lane membership.
"""

from __future__ import annotations

import time

from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE
from pipeline.contactability.official import apply_official_public_contacts
from pipeline.db.database import init_db
from pipeline.entity.report import score_guardrails
from pipeline.sales_lanes.lanes import PLUMBING_CORE
from pipeline.sales_trial.abc import review_abc_water_works
from pipeline.sales_trial.callability import NO_ACTIONABLE_CONTACT, persist_callability
from pipeline.sales_trial.delta import resolve_arizona_delta
from pipeline.sales_trial.kpis import after_kpis, before_kpis, existing_inventory
from pipeline.sales_trial.report import write_phase4g_report
from pipeline.sales_trial.sheet import write_call_sheet
from pipeline.sales_trial.snapshot import cohort_company_ids, freeze_plumbing_core, load_plumbing_book
from pipeline.sales_trial.trial import build_trial_book


def _lane_fingerprint(conn, company_ids: list[int]) -> list[tuple]:
    if not company_ids:
        return []
    ph = ",".join("?" * len(company_ids))
    return list(
        conn.execute(
            f"""
            SELECT company_id, lane_key, fit, presentable
            FROM company_sales_lanes
            WHERE company_id IN ({ph})
            ORDER BY company_id, lane_key
            """,
            company_ids,
        )
    )


def run(conn) -> dict:
    before = score_guardrails(conn)
    t0 = time.time()
    book = load_plumbing_book(conn)
    if not book:
        raise RuntimeError("PLUMBING_CORE book is empty; run pipeline.sales_lanes first.")
    frozen_ids = cohort_company_ids(book)
    lanes_before = _lane_fingerprint(conn, frozen_ids)
    frozen = freeze_plumbing_core(conn)
    inventory = existing_inventory(conn, frozen)
    before_cov = before_kpis(frozen)
    enrich = apply_official_public_contacts(conn, frozen_ids)
    delta = resolve_arizona_delta(conn)
    abc = review_abc_water_works(conn)
    enriched = persist_callability(conn, frozen)
    after_cov = after_kpis(conn, enriched)
    trial = build_trial_book(conn, enriched)
    html_path = write_call_sheet(conn, enriched, trial)
    missing = [
        {
            "presentation_rank": r["presentation_rank"],
            "canonical_name": r["canonical_name"],
            "reason": r.get("callability_reasons"),
        }
        for r in enriched
        if r.get("callability") == NO_ACTIONABLE_CONTACT or not (r.get("after") or {}).get("actionable")
    ]
    elapsed = round(time.time() - t0, 1)
    after = score_guardrails(conn)
    if before != after:
        raise RuntimeError(f"Phase 4G mutated ranking/CRM guardrails: {before} -> {after}")
    lanes_after = _lane_fingerprint(conn, frozen_ids)
    if lanes_before != lanes_after:
        raise RuntimeError("Phase 4G mutated company_sales_lanes for the frozen cohort")
    ranks = [r["presentation_rank"] for r in frozen]
    names = [r["canonical_name"] for r in frozen]
    live = load_plumbing_book(conn)
    if [r["presentation_rank"] for r in live] != ranks or [r["canonical_name"] for r in live] != names:
        raise RuntimeError("Phase 4G changed frozen PLUMBING_CORE membership or order")
    path = write_phase4g_report(
        frozen=frozen,
        inventory=inventory,
        enrich=enrich,
        before=before_cov,
        after=after_cov,
        callability_rows=enriched,
        delta=delta,
        abc=abc,
        trial=trial,
        html_path=html_path,
        elapsed_s=elapsed,
        before_guard=before,
        after_guard=after,
        missing=missing,
    )
    return {
        "elapsed_s": elapsed,
        "frozen_n": len(frozen),
        "enrich": enrich,
        "before": before_cov,
        "after": after_cov,
        "missing": missing,
        "trial": [a["company"] for a in trial.get("accounts") or []],
        "report": str(path),
        "html": str(html_path),
        "guardrails": after,
        "profile": CUSTOMER_RELEVANCE_PROFILE,
        "lane": PLUMBING_CORE,
    }


def main() -> int:
    conn = init_db()
    try:
        result = run(conn)
        print("phase4g", result)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
