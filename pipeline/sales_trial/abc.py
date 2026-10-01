"""ABC Water Works lane presentation review. Does not retune scores or lanes."""

from __future__ import annotations

import json
import sqlite3

from pipeline.config.settings import SALES_TRIAL_VERSION
from pipeline.db.database import now_iso
from pipeline.sales_gate.evidence import demand_counts
from pipeline.sales_lanes.lanes import FIRE_BACKFLOW, MULTI_TRADE, PLUMBING_CORE


def review_abc_water_works(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT c.id, c.display_name, a.account_priority_score, a.trade_identity,
               a.primary_demand_category, a.relevant_30d, a.relevant_90d, a.relevant_180d
        FROM companies c
        JOIN company_customer_priority a
          ON a.company_id=c.id AND a.profile_key='plumbing_supply'
        WHERE c.display_name LIKE 'ABC WATER WORKS%'
        ORDER BY a.account_priority_score DESC
        LIMIT 1
        """,
    ).fetchone()
    if row is None:
        return {"found": False}
    cid = int(row["id"])
    demand_row = conn.execute(
        """
        SELECT demand_categories FROM company_customer_priority
        WHERE company_id=? AND profile_key='plumbing_supply'
        """,
        (cid,),
    ).fetchone()
    demand = demand_counts(None if demand_row is None else demand_row["demand_categories"])
    lanes = [
        dict(r)
        for r in conn.execute(
            "SELECT lane_key, fit, presentable FROM company_sales_lanes WHERE company_id=?",
            (cid,),
        )
    ]
    licenses = [
        dict(r)
        for r in conn.execute(
            """
            SELECT l.normalized_license_number, l.normalized_class, l.raw_class
            FROM roc_company_matches m
            LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
            WHERE m.company_id=?
            """,
            (cid,),
        )
    ]
    fire = int(demand.get("fire_backflow") or 0)
    unknown_wet = int(demand.get("unknown_wet") or 0)
    plumbing = int(demand.get("plumbing_service") or 0) + int(demand.get("plumbing_fixture") or 0)
    recommendation = (
        "DUAL_LANE_FIRE_PRIMARY. Keep the existing PLUMBING_CORE book row frozen. "
        "For salesperson presentation, treat ABC Water Works as FIRE_BACKFLOW primary "
        "with PLUMBING_CORE secondary. License identity is CR-37, but recent demand is "
        "dominated by backflow testing (FRYS 45 BACKFLOW) rather than fixture plumbing. "
        "Do not change account_priority_score or rewrite company_sales_lanes in this phase."
    )
    evidence = {
        "company_id": cid,
        "display_name": row["display_name"],
        "account_priority_score": row["account_priority_score"],
        "trade_identity": row["trade_identity"],
        "primary_demand_category": row["primary_demand_category"],
        "demand_counts": demand,
        "fire_backflow": fire,
        "unknown_wet": unknown_wet,
        "plumbing_service_fixture": plumbing,
        "lanes": lanes,
        "licenses": licenses,
        "activity": {
            "30d": row["relevant_30d"],
            "90d": row["relevant_90d"],
            "180d": row["relevant_180d"],
        },
    }
    conn.execute(
        """
        INSERT INTO sales_presentation_notes (
            company_id, topic, recommendation, evidence, model_version, created_at
        ) VALUES (?,?,?,?,?,?)
        ON CONFLICT(company_id, topic, model_version) DO UPDATE SET
            recommendation=excluded.recommendation, evidence=excluded.evidence
        """,
        (
            cid,
            "abc_water_works_lane",
            recommendation,
            json.dumps(evidence, default=str),
            SALES_TRIAL_VERSION,
            now_iso(),
        ),
    )
    conn.commit()
    return {
        "found": True,
        "company_id": cid,
        "recommendation": recommendation,
        "evidence": evidence,
        "lanes_unchanged": True,
        "score_unchanged": True,
        "present_as": {
            "primary": FIRE_BACKFLOW,
            "secondary": PLUMBING_CORE,
            "also": MULTI_TRADE,
        },
    }
