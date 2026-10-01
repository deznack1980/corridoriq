"""Phase 4E CLI.

    python -m pipeline.sales_gate

Writes identity / person / sales-readiness review tables and an internal report.
Does not change dashboard ranking, CRM, or score columns.
"""

from __future__ import annotations

import argparse
import time

from pipeline.contactability.official import apply_official_public_contacts
from pipeline.db.database import init_db
from pipeline.entity.report import score_guardrails
from pipeline.relevance.account_report import top_accounts
from pipeline.sales_gate.audit import write_account_reviews
from pipeline.sales_gate.identity import write_identity_reviews
from pipeline.sales_gate.persons import review_person_accounts
from pipeline.sales_gate.report import write_phase4e_report


def run(conn) -> dict:
    before = score_guardrails(conn)
    t0 = time.time()
    identity_stats = write_identity_reviews(conn)
    person_stats = review_person_accounts(conn, limit=50)
    top = top_accounts(conn, limit=50)
    ids = [int(r["company_id"]) for r in top]
    # Also apply Umbrella / Hiller even if just outside Top 25.
    extra = conn.execute(
        """
        SELECT id FROM companies
        WHERE lifecycle_state='active'
          AND (
            upper(display_name) LIKE '%UMBRELLA PLUMBING%'
            OR upper(display_name) LIKE '%HILLER COMPAN%'
          )
        """
    )
    for row in extra:
        cid = int(row["id"])
        if cid not in ids:
            ids.append(cid)
    official_stats = apply_official_public_contacts(conn, ids)
    accounts = write_account_reviews(conn, limit=25)
    elapsed = round(time.time() - t0, 1)
    after = score_guardrails(conn)
    if before != after:
        raise RuntimeError(f"Phase 4E mutated ranking/CRM guardrails: {before} -> {after}")
    path = write_phase4e_report(
        conn,
        accounts=accounts,
        identity_stats=identity_stats,
        person_stats=person_stats,
        official_stats=official_stats,
        elapsed_s=elapsed,
        before_guard=before,
        after_guard=after,
    )
    return {
        "elapsed_s": elapsed,
        "identity": identity_stats,
        "persons": person_stats,
        "official": official_stats,
        "accounts": len(accounts),
        "report": str(path),
        "guardrails": after,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    conn = init_db()
    try:
        result = run(conn)
        print("phase4e", result)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
