"""Phase 4D CLI.

    python -m pipeline.entity

Reviews ROC duplicates, writes canonical links (no merges), imports existing
contacts, applies a controlled official-site pass, and writes an internal report.
Does not change dashboard ranking, CRM, or score columns.
"""

from __future__ import annotations

import argparse
import time

from pipeline.contactability.import_research import import_research_csvs
from pipeline.contactability.inventory import ingest_existing_contacts
from pipeline.contactability.official import apply_official_public_contacts
from pipeline.contactability.store import share_channels_across_canonical
from pipeline.db.database import init_db
from pipeline.entity.canonical import run_entity_resolution
from pipeline.entity.refine import refine_known_ambiguities
from pipeline.entity.report import score_guardrails, write_phase4d_report
from pipeline.relevance.account_report import top_accounts


def run(conn, *, with_contacts: bool = True) -> dict:
    before = score_guardrails(conn)
    t0 = time.time()
    entity_stats = run_entity_resolution(conn)
    inventory_stats: dict = {}
    research_stats: dict = {}
    official_stats: dict = {}
    if with_contacts:
        inventory_stats = ingest_existing_contacts(conn)
        research_stats = import_research_csvs(conn)
        top = top_accounts(conn, limit=100)
        ids = [int(r["company_id"]) for r in top]
        official_stats = apply_official_public_contacts(conn, ids)
        official_stats["canonical_share"] = share_channels_across_canonical(conn)
        entity_stats["refine"] = refine_known_ambiguities(conn, ids)
    else:
        entity_stats["refine"] = refine_known_ambiguities(conn)
    elapsed = time.time() - t0
    after = score_guardrails(conn)
    if before != after:
        raise RuntimeError(f"Phase 4D mutated ranking/CRM guardrails: {before} -> {after}")
    path = write_phase4d_report(
        conn,
        entity_stats=entity_stats,
        inventory_stats=inventory_stats,
        research_stats=research_stats,
        official_stats=official_stats,
        elapsed_s=round(elapsed, 1),
        before_guard=before,
        after_guard=after,
    )
    return {
        "elapsed_s": round(elapsed, 1),
        "entity": entity_stats,
        "inventory": inventory_stats,
        "research": research_stats,
        "official": official_stats,
        "report": str(path),
        "guardrails": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-contacts", action="store_true")
    args = parser.parse_args()
    conn = init_db()
    try:
        result = run(conn, with_contacts=not args.skip_contacts)
        print("phase4d", result)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
