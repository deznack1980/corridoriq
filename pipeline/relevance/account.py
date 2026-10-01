"""Shadow account-priority scores for plumbing_supply.

Does not write opportunity_score or project_customer_relevance.
Does not rewrite contractor-intel-v2 rows.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from pipeline.config.settings import (
    ACCOUNT_PRIORITY_MODEL_VERSION,
    CUSTOMER_RELEVANCE_PROFILE,
)
from pipeline.db.database import now_iso
from pipeline.relevance.classify import seed_relevance_profiles
from pipeline.relevance.demand_taxonomy import (
    RELEVANT_CATEGORIES,
    demand_quality,
    label_demand,
    primary_category,
)
from pipeline.relevance.identity import (
    infer_trade_identity,
    reserved_license_payload,
)
from pipeline.relevance.profiles import PROFILE_PLUMBING_SUPPLY

W_IDENTITY = 0.22
W_ACTIVITY = 0.18
W_RECENCY = 0.28
W_DEMAND = 0.14
W_QUALITY = 0.10
W_CONFIDENCE = 0.08

HIGH_BAND = 70.0
RELEVANT_FLOOR = 40.0
_BATCH = 400


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text[:19], fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _best_date(row: dict) -> datetime | None:
    for key in ("opportunity_date", "issued_date", "filed_date"):
        dt = _parse_date(row.get(key))
        if dt is not None:
            return dt
    return None


def volume_score(n_relevant: int, n_high: int) -> float:
    """Diminishing returns. 50 jobs is not 10x five jobs."""
    rel = 100.0 * (1.0 - math.exp(-max(0, n_relevant) / 12.0))
    high = 100.0 * (1.0 - math.exp(-max(0, n_high) / 8.0))
    return round(0.45 * rel + 0.55 * high, 1)


def recency_score(
    *,
    n30: int,
    n90: int,
    n180: int,
    n365: int,
    most_recent: datetime | None,
    as_of: datetime,
) -> float:
    if most_recent is None:
        return 6.0
    days = max(0, (as_of - most_recent).days)
    if days <= 30:
        freshness = 100.0
    elif days <= 90:
        freshness = 82.0
    elif days <= 180:
        freshness = 55.0
    elif days <= 365:
        freshness = 28.0
    else:
        freshness = 8.0
    velocity = min(100.0, n30 * 16.0 + n90 * 3.5 + n180 * 0.8 + n365 * 0.15)
    if days > 365:
        velocity = min(velocity, 12.0)
    return round(0.58 * freshness + 0.42 * velocity, 1)


def project_quality_score(scores: list[float], recent_scores: list[float]) -> float:
    """Not MAX. Median + upper-band + recent quality."""
    usable = [s for s in scores if s >= RELEVANT_FLOOR]
    if not usable:
        return 8.0 if scores else 6.0
    usable.sort()
    median = statistics.median(usable)
    p80 = usable[min(len(usable) - 1, int(len(usable) * 0.8))]
    recent = recent_scores or usable
    recent_mean = sum(recent) / len(recent)
    return round(0.40 * median + 0.35 * p80 + 0.25 * recent_mean, 1)


def _why(identity: str, n90: int, n30: int, primary: str | None, most_recent: str | None) -> str:
    bits = [identity.replace("_", " ")]
    if n30:
        bits.append(f"{n30} relevant jobs in 30d")
    elif n90:
        bits.append(f"{n90} relevant jobs in 90d")
    else:
        bits.append("no relevant activity in 90d")
    if primary:
        bits.append(f"primary demand {primary.replace('_', ' ')}")
    if most_recent:
        bits.append(f"latest {most_recent[:10]}")
    return "; ".join(bits) + "."


def _as_of(value: datetime | None) -> datetime:
    if value is not None:
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def score_company_accounts(
    conn: sqlite3.Connection,
    *,
    profile_key: str | None = None,
    model_version: str | None = None,
    as_of: datetime | None = None,
) -> dict:
    """Rebuild shadow account priority from existing project relevance rows."""
    profile_key = profile_key or CUSTOMER_RELEVANCE_PROFILE
    model_version = model_version or ACCOUNT_PRIORITY_MODEL_VERSION
    if profile_key != PROFILE_PLUMBING_SUPPLY:
        raise ValueError(f"only {PROFILE_PLUMBING_SUPPLY} is enabled")
    seed_relevance_profiles(conn)
    clock = _as_of(as_of)
    generated = now_iso()

    caps_by: dict[int, list[dict]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT company_id, capability, confidence, attribution_role, capability_class
        FROM company_capabilities
        """
    ):
        caps_by[int(row["company_id"])].append(dict(row))

    names = {
        int(r["id"]): r["display_name"]
        for r in conn.execute("SELECT id, display_name FROM companies")
    }

    projects = conn.execute(
        """
        SELECT r.project_id, r.company_id, r.relevance_score, r.demand_score,
               pr.project_category, pr.opportunity_date, pr.opportunity_score,
               p.permit_type, p.description, p.project_description,
               p.issued_date, p.filed_date
        FROM project_customer_relevance r
        JOIN projects pr ON pr.id = r.project_id
        JOIN permits p ON p.id = pr.permit_id
        WHERE r.profile_key=? AND r.company_id IS NOT NULL
        """,
        (profile_key,),
    )

    buckets: dict[int, list[dict]] = defaultdict(list)
    label_rows: list[tuple] = []
    demand_hist: dict[str, int] = defaultdict(int)

    for raw in projects:
        row = dict(raw)
        labels = label_demand(
            permit_type=row["permit_type"],
            description=row["description"],
            project_category=row["project_category"],
            extra_text=row["project_description"],
        )
        primary = primary_category(labels)
        row["labels"] = labels
        row["primary"] = primary
        row["event_date"] = _best_date(row)
        buckets[int(row["company_id"])].append(row)
        for lab in labels:
            demand_hist[lab] += 1
        label_rows.append(
            (
                row["project_id"],
                profile_key,
                json.dumps(labels),
                primary,
                model_version,
                generated,
            )
        )

    conn.execute("DELETE FROM project_demand_labels WHERE profile_key=?", (profile_key,))
    conn.execute("DELETE FROM company_customer_priority WHERE profile_key=?", (profile_key,))

    for i in range(0, len(label_rows), _BATCH):
        conn.executemany(
            """
            INSERT INTO project_demand_labels (
                project_id, profile_key, categories, primary_category,
                model_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            label_rows[i:i + _BATCH],
        )

    account_rows: list[tuple] = []
    for company_id, jobs in buckets.items():
        caps = caps_by.get(company_id, [])
        ident = infer_trade_identity(
            company_name=names.get(company_id),
            caps=caps,
            license_payload=reserved_license_payload(conn, company_id),
        )
        counts: dict[str, int] = defaultdict(int)
        scores: list[float] = []
        recent_scores: list[float] = []
        n30 = n90 = n180 = n365 = n_older = 0
        n_high = 0
        n_rel = 0
        n_plum = 0
        n_gas = 0
        most_recent: datetime | None = None
        best_job = None
        for job in jobs:
            labels = job["labels"]
            rel = float(job["relevance_score"] or 0)
            scores.append(rel)
            wet = bool(set(labels) & RELEVANT_CATEGORIES) or rel >= RELEVANT_FLOOR
            if not wet:
                continue
            n_rel += 1
            if rel >= HIGH_BAND:
                n_high += 1
            if "fuel_gas" in labels:
                n_gas += 1
            if any(lab.startswith("plumbing_") or lab in {"water_heater", "commercial_plumbing"} for lab in labels):
                n_plum += 1
            for lab in labels:
                counts[lab] += 1
            dt = job["event_date"]
            if dt is not None:
                if most_recent is None or dt > most_recent:
                    most_recent = dt
                    best_job = job
                days = (clock - dt).days
                if days <= 30:
                    n30 += 1
                    recent_scores.append(rel)
                if days <= 90:
                    n90 += 1
                    if days > 30:
                        recent_scores.append(rel)
                if days <= 180:
                    n180 += 1
                if days <= 365:
                    n365 += 1
                else:
                    n_older += 1
            if best_job is None or rel > float(best_job["relevance_score"] or 0):
                if dt is None or most_recent is None:
                    best_job = job
        if best_job is None and jobs:
            best_job = max(jobs, key=lambda j: float(j["relevance_score"] or 0))

        identity_s = ident["trade_identity_score"]
        activity_s = volume_score(n_rel, n_high)
        recency_s = recency_score(
            n30=n30, n90=n90, n180=n180, n365=n365,
            most_recent=most_recent, as_of=clock,
        )
        demand_s = demand_quality(counts)
        quality_s = project_quality_score(scores, recent_scores)
        conf_s = ident["confidence"]
        name = names.get(company_id) or ""
        fixture_like = (
            counts.get("plumbing_fixture", 0)
            + counts.get("plumbing_service", 0)
            + counts.get("water_heater", 0)
            + counts.get("fuel_gas", 0)
        )
        commercial = counts.get("commercial_plumbing", 0)
        if (
            ident["trade_identity"] == "plumbing_specialist"
            and commercial >= 5
            and commercial > fixture_like
            and not re.search(r"\bplumb|\bgas\b|\bpropane\b|\bpipe", name, re.I)
        ):
            ident["trade_identity"] = "gc_with_plumbing_demand"
            ident["trade_identity_score"] = 42.0
            identity_s = 42.0
            conf_s = min(conf_s, 70.0)
        if n90 == 0:
            activity_s = round(activity_s * 0.42, 1)
        if ident["trade_identity"] in {"municipality", "incidental_trade"}:
            activity_s = min(activity_s, 25.0)
            recency_s = min(recency_s, 35.0)
        if ident["trade_identity"] == "gc_with_plumbing_demand" and re.search(r"\bhomes?\b", name, re.I):
            demand_s = min(demand_s, 32.0)
            activity_s = min(activity_s, 40.0)
        total = round(
            identity_s * W_IDENTITY
            + activity_s * W_ACTIVITY
            + recency_s * W_RECENCY
            + demand_s * W_DEMAND
            + quality_s * W_QUALITY
            + conf_s * W_CONFIDENCE,
            1,
        )
        strongest = None
        if caps:
            strongest = max(
                caps,
                key=lambda c: (
                    1 if c.get("capability") in {"plumbing", "fuel_gas"} else 0,
                    float(c.get("confidence") or 0),
                ),
            )
        recent_iso = most_recent.date().isoformat() if most_recent else None
        primary = primary_category(list(counts.keys())) if counts else None
        account_rows.append(
            (
                profile_key,
                company_id,
                total,
                ident["trade_identity"],
                identity_s,
                activity_s,
                recency_s,
                demand_s,
                quality_s,
                conf_s,
                n_rel,
                n_high,
                n_plum,
                n_gas,
                n30,
                n90,
                n180,
                n365,
                n_older,
                recent_iso,
                primary,
                json.dumps(dict(counts)),
                None if strongest is None else strongest.get("capability"),
                None if strongest is None else strongest.get("confidence"),
                ident["role"],
                None if strongest is None else strongest.get("capability_class"),
                ident["identity_basis"],
                _why(ident["trade_identity"], n90, n30, primary, recent_iso),
                model_version,
                generated,
            )
        )

    for i in range(0, len(account_rows), _BATCH):
        conn.executemany(
            """
            INSERT INTO company_customer_priority (
                profile_key, company_id, account_priority_score, trade_identity,
                trade_identity_score, activity_score, recency_score,
                demand_quality_score, project_quality_score, confidence_score,
                active_relevant_project_count, high_relevance_project_count,
                plumbing_project_count, fuel_gas_project_count,
                relevant_30d, relevant_90d, relevant_180d, relevant_365d,
                relevant_older, most_recent_relevant_date,
                primary_demand_category, demand_categories,
                strongest_capability, capability_confidence,
                attribution_role, capability_class, identity_basis, why_now,
                model_version, generated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            account_rows[i:i + _BATCH],
        )
    conn.commit()
    return {
        "profile_key": profile_key,
        "model_version": model_version,
        "accounts": len(account_rows),
        "labeled_projects": len(label_rows),
        "demand_histogram": dict(demand_hist),
        "opportunity_rows_written": 0,
        "relevance_rows_written": 0,
    }


def score_distribution(scores: Iterable[float]) -> dict:
    vals = sorted(scores)
    if not vals:
        return {"n": 0, "unique": 0, "largest_tie": 0}
    from collections import Counter
    c = Counter(round(v, 1) for v in vals)
    return {
        "n": len(vals),
        "unique": len(c),
        "largest_tie": max(c.values()),
        "rank1": vals[-1],
        "rank10": vals[-10] if len(vals) >= 10 else vals[0],
        "rank25": vals[-25] if len(vals) >= 25 else vals[0],
        "rank50": vals[-50] if len(vals) >= 50 else vals[0],
        "rank100": vals[-100] if len(vals) >= 100 else vals[0],
        "spread_1_100": round(vals[-1] - (vals[-100] if len(vals) >= 100 else vals[0]), 1),
    }
