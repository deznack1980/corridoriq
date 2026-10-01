"""Simulate ROC identity impact without writing account_priority_score."""

from __future__ import annotations

import json
import sqlite3

from pipeline.config.settings import CUSTOMER_RELEVANCE_PROFILE
from pipeline.relevance.account import (
    W_ACTIVITY,
    W_CONFIDENCE,
    W_DEMAND,
    W_IDENTITY,
    W_QUALITY,
    W_RECENCY,
)
from pipeline.relevance.identity import IDENTITY_SCORE
from pipeline.roc.classifications import CAP_FIRE, CAP_FUEL_GAS, CAP_GC, CAP_PLUMBING
from pipeline.roc.validate import CONFIRMED, CONTRADICTED, PARTIAL, ROC_ADDS

_GC = "gc_with_plumbing_demand"
_OTHER = "other_trade"
_RECUR = "recurring_plumbing"


def simulate_account_priority(conn: sqlite3.Connection, *, profile_key: str | None = None) -> dict:
    profile_key = profile_key or CUSTOMER_RELEVANCE_PROFILE
    current = [
        dict(r)
        for r in conn.execute(
            """
            SELECT a.*, c.display_name
            FROM company_customer_priority a
            JOIN companies c ON c.id = a.company_id
            WHERE a.profile_key=?
            ORDER BY a.account_priority_score DESC, a.relevant_90d DESC, a.company_id
            """,
            (profile_key,),
        )
    ]
    validations = {
        int(r["company_id"]): dict(r)
        for r in conn.execute(
            "SELECT * FROM roc_identity_validations WHERE profile_key=?",
            (profile_key,),
        )
    }
    simulated = []
    moved = []
    for rank, row in enumerate(current, start=1):
        row["current_rank"] = rank
        sim = _simulate_row(row, validations.get(int(row["company_id"])))
        simulated.append(sim)
    simulated.sort(key=lambda r: (-r["simulated_score"], -r["relevant_90d"], r["company_id"]))
    for new_rank, row in enumerate(simulated, start=1):
        row["simulated_rank"] = new_rank
        delta = row["current_rank"] - new_rank
        row["rank_delta"] = delta
        if delta != 0 or abs(row["simulated_score"] - row["account_priority_score"]) >= 0.05:
            moved.append(row)
    return {
        "current_top": current[:100],
        "simulated_top": simulated[:100],
        "moved": sorted(moved, key=lambda r: -abs(r["rank_delta"]))[:50],
        "would_write_account_priority": False,
        "top25_identity_flips": [
            r for r in simulated[:25]
            if r["simulated_identity"] != r["trade_identity"]
        ],
    }


def _simulate_row(row: dict, validation: dict | None) -> dict:
    identity = row["trade_identity"]
    identity_s = float(row["trade_identity_score"])
    conf_s = float(row["confidence_score"])
    activity_s = float(row["activity_score"])
    recency_s = float(row["recency_score"])
    demand_s = float(row["demand_quality_score"])
    quality_s = float(row["project_quality_score"])
    reason = "unchanged"
    caps = []
    result = None
    if validation:
        result = validation.get("validation_result")
        try:
            caps = json.loads(validation.get("roc_capabilities") or "[]")
        except json.JSONDecodeError:
            caps = []
        delta = float(validation.get("confidence_delta") or 0)
        conf_s = max(0.0, min(100.0, conf_s + delta))
        if result == CONFIRMED:
            reason = "roc_confirmed_identity"
        elif result == CONTRADICTED and identity in {"plumbing_specialist", "recurring_plumbing"}:
            identity = _GC
            identity_s = IDENTITY_SCORE[_GC]
            reason = "would_demote_to_gc_demand"
        elif result == ROC_ADDS and identity in {"unknown", "incidental_trade", "other_trade"}:
            if CAP_PLUMBING in caps or CAP_FUEL_GAS in caps:
                identity = _RECUR
                identity_s = IDENTITY_SCORE[_RECUR]
                reason = "would_promote_from_roc_trade_license"
        elif result == PARTIAL and CAP_FIRE in caps and identity == "plumbing_specialist" and CAP_PLUMBING not in caps:
            identity = _OTHER
            identity_s = IDENTITY_SCORE[_OTHER]
            reason = "would_treat_as_fire_protection_segment"
        elif result == CONTRADICTED and CAP_GC in caps:
            identity = _GC
            identity_s = IDENTITY_SCORE[_GC]
            reason = "would_align_to_gc_license"
    total = round(
        identity_s * W_IDENTITY
        + activity_s * W_ACTIVITY
        + recency_s * W_RECENCY
        + demand_s * W_DEMAND
        + quality_s * W_QUALITY
        + conf_s * W_CONFIDENCE,
        1,
    )
    out = dict(row)
    out["simulated_score"] = total
    out["simulated_identity"] = identity
    out["simulated_confidence"] = round(conf_s, 1)
    out["simulation_reason"] = reason
    out["validation_result"] = result
    return out
