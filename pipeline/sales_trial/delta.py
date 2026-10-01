"""Arizona Delta Mechanical vs AZ Delta Mechanical — presentation collapse only."""

from __future__ import annotations

import json
import sqlite3

from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE, SALES_TRIAL_VERSION
from pipeline.contactability.store import share_channels_across_canonical
from pipeline.db.database import now_iso
from pipeline.entity.canonical import link_presentation_pair


DELTA_EVIDENCE = {
    "same_trade": "mechanical / plumbing wet-side",
    "arizona_delta_licenses": "314342 R-37R; 315925 C-37",
    "shared_address": "6056 E Baseline Rd #155, Mesa, AZ",
    "related_legal": "Delta Mechanical Inc holds overlapping licenses at the same address (ROC phone 480-898-0007).",
    "az_variant": "AZ Delta Mechanical is a permit-name abbreviation with no independent ROC row.",
    "do_not_merge": True,
}


def _find(conn: sqlite3.Connection, needle: str) -> dict | None:
    row = conn.execute(
        """
        SELECT id, display_name, city, lifecycle_state, merged_into_id
        FROM companies WHERE display_name LIKE ? AND lifecycle_state='active'
        ORDER BY id LIMIT 1
        """,
        (needle,),
    ).fetchone()
    return dict(row) if row else None


def combined_history(conn: sqlite3.Connection, company_ids: list[int]) -> dict:
    if not company_ids:
        return {}
    ph = ",".join("?" * len(company_ids))
    pri = conn.execute(
        f"""
        SELECT company_id, account_priority_score, relevant_30d, relevant_90d,
               relevant_180d, active_relevant_project_count, primary_demand_category
        FROM company_customer_priority
        WHERE profile_key='plumbing_supply' AND company_id IN ({ph})
        """,
        company_ids,
    ).fetchall()
    n_proj = conn.execute(
        f"SELECT COUNT(*) n FROM projects WHERE contractor_company_id IN ({ph})",
        company_ids,
    ).fetchone()["n"]
    n_rel = conn.execute(
        f"""
        SELECT COUNT(*) n FROM project_customer_relevance
        WHERE profile_key=? AND company_id IN ({ph})
        """,
        (CUSTOMER_RELEVANCE_PROFILE, *company_ids),
    ).fetchone()["n"]
    return {
        "member_ids": company_ids,
        "priority_rows": [dict(r) for r in pri],
        "combined_priority": round(sum(float(r["account_priority_score"] or 0) for r in pri), 1),
        "project_rows": int(n_proj),
        "relevance_rows": int(n_rel),
        "combined_90d": sum(int(r["relevant_90d"] or 0) for r in pri),
        "combined_180d": sum(int(r["relevant_180d"] or 0) for r in pri),
        "note": (
            "Combined figures are additive across raw rows that likely represent one "
            "commercial account. Scores were not recalculated."
        ),
    }


def resolve_arizona_delta(conn: sqlite3.Connection) -> dict:
    arizona = _find(conn, "ARIZONA DELTA MECHANICAL%")
    az = _find(conn, "AZ Delta Mechanical%")
    delta_inc = _find(conn, "DELTA MECHANICAL INC%")
    if not arizona or not az:
        return {"linked": False, "reason": "missing_raw_rows", "arizona": arizona, "az": az}
    before_merged = conn.execute(
        "SELECT COUNT(*) n FROM companies WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
    ).fetchone()["n"]
    before_fk = list(
        conn.execute("SELECT id, contractor_company_id FROM projects ORDER BY id LIMIT 20")
    )
    link = link_presentation_pair(
        conn,
        company_ids=[int(arizona["id"]), int(az["id"])],
        canonical_name=arizona["display_name"],
        evidence=DELTA_EVIDENCE,
        relationship_type="name_variant",
        confidence=93.0,
    )
    shared = share_channels_across_canonical(conn)
    after_merged = conn.execute(
        "SELECT COUNT(*) n FROM companies WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
    ).fetchone()["n"]
    after_fk = list(
        conn.execute("SELECT id, contractor_company_id FROM projects ORDER BY id LIMIT 20")
    )
    if after_merged != before_merged or after_fk != before_fk:
        raise RuntimeError("Arizona Delta presentation link mutated companies or project FKs")
    related = None if delta_inc is None else int(delta_inc["id"])
    ids = [int(arizona["id"]), int(az["id"])]
    now = now_iso()
    conn.execute(
        """
        INSERT INTO sales_presentation_notes (
            company_id, topic, recommendation, evidence, model_version, created_at
        ) VALUES (?,?,?,?,?,?)
        ON CONFLICT(company_id, topic, model_version) DO UPDATE SET
            recommendation=excluded.recommendation, evidence=excluded.evidence
        """,
        (
            int(arizona["id"]),
            "arizona_delta_collapse",
            (
                "PRESENTATION_COLLAPSE. Arizona Delta Mechanical Inc and AZ Delta Mechanical "
                "are the same commercial account (AZ abbreviation + same Mesa Baseline #155 "
                "cluster). Do not merge raw company rows. Delta Mechanical Inc at the same "
                "address is related but was not folded into the frozen Top 25."
            ),
            json.dumps(
                {
                    **DELTA_EVIDENCE,
                    "arizona_id": int(arizona["id"]),
                    "az_id": int(az["id"]),
                    "related_delta_mechanical_inc_id": related,
                    "combined": combined_history(conn, ids + ([related] if related else [])),
                }
            ),
            SALES_TRIAL_VERSION,
            now,
        ),
    )
    conn.commit()
    return {
        "linked": True,
        "arizona": arizona,
        "az": az,
        "related_delta_mechanical_inc": delta_inc,
        "link": link,
        "channels_shared": shared,
        "combined_frozen_pair": combined_history(conn, ids),
        "combined_with_delta_inc": combined_history(conn, ids + ([related] if related else [])),
        "merged_companies": after_merged,
    }
