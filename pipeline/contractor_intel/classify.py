"""Write contractor capabilities from permit/project evidence.

Never updates ``projects.opportunity_score`` or other scoring columns.
Rebuilds capability rows for this model version (idempotent).
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from pipeline.company_resolution.normalize import is_placeholder_name
from pipeline.config.settings import (
    CONTRACTOR_INTEL_MIN_CONFIDENCE,
    CONTRACTOR_INTEL_MODEL_VERSION,
)
from pipeline.contractor_intel.confidence import (
    Evidence,
    capability_class,
    compute_confidence,
    dominant_source,
)
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.contractor_intel.roles import infer_profile, attribute_observation
from pipeline.contractor_intel.signals import extract_signals
from pipeline.contractor_intel.taxonomy import (
    GC_INCIDENTAL_CAP,
    NON_TRADE_ROLES,
    SIGNAL_DERIVED,
    TRADE_CAPABILITIES,
)

_SELECT = """
SELECT p.id AS permit_id,
       pr.id AS project_id,
       p.contractor_company_id AS company_id,
       p.permit_type,
       p.description,
       p.project_description,
       p.issued_date,
       p.filed_date,
       p.general_contractor_name,
       p.plumbing_contractor_name,
       pr.project_category,
       pr.opportunity_date,
       pr.owner_company_id,
       pr.developer_company_id,
       pr.architect_company_id,
       pr.engineer_company_id,
       c.display_name,
       c.legal_name,
       c.license_number,
       c.license_status,
       c.lifecycle_state
FROM permits p
JOIN projects pr ON pr.permit_id = p.id
JOIN companies c ON c.id = p.contractor_company_id
WHERE p.contractor_company_id IS NOT NULL
"""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _evidence_date(issued: str | None, filed: str | None, opp: str | None) -> str | None:
    for value in (issued, filed, opp):
        if value and str(value).strip():
            return str(value).strip()[:10]
    return None


def _company_roles(conn: sqlite3.Connection) -> dict[int, list[str]]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='company_roles'"
    ).fetchone()
    if not exists:
        return {}
    out: dict[int, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT company_id, role_type FROM company_roles"):
        out[int(row["company_id"])].append(str(row["role_type"]))
    return out


def _collect(conn: sqlite3.Connection) -> dict[int, dict]:
    """company_id -> bucket with observations, then profiles."""
    role_map = _company_roles(conn)
    buckets: dict[int, dict] = {}
    rows = conn.execute(_SELECT)
    for row in rows:
        if row["lifecycle_state"] and row["lifecycle_state"] != "active":
            continue
        name = row["display_name"] or row["legal_name"]
        if is_placeholder_name(name):
            continue
        cid = int(row["company_id"])
        bucket = buckets.setdefault(
            cid,
            {
                "name": name,
                "permits": set(),
                "permit_types": [],
                "descriptions": [],
                "owner_links": 0,
                "observations": [],
            },
        )
        bucket["permits"].add(int(row["permit_id"]))
        if row["permit_type"]:
            bucket["permit_types"].append(row["permit_type"])
        blob = " ".join(
            part for part in (row["description"], row["project_description"]) if part
        )
        if blob:
            bucket["descriptions"].append(blob[:400])
        if row["owner_company_id"] and int(row["owner_company_id"]) == cid:
            bucket["owner_links"] += 1
        date = _evidence_date(
            row["issued_date"], row["filed_date"], row["opportunity_date"]
        )
        signals = extract_signals(
            permit_type=row["permit_type"],
            description=row["description"],
            extra_text=row["project_description"],
            project_category=row["project_category"],
            company_name=name,
            license_number=row["license_number"],
        )
        seen_here: set[tuple[str, str, str]] = set()
        for sig in signals:
            key = (sig.capability, sig.signal_type, sig.signal_value)
            if key in seen_here:
                continue
            seen_here.add(key)
            bucket["observations"].append(
                {
                    "sig": sig,
                    "permit_id": int(row["permit_id"]),
                    "project_id": int(row["project_id"]),
                    "permit_type": row["permit_type"],
                    "description": blob,
                    "gc_name": row["general_contractor_name"],
                    "plum_name": row["plumbing_contractor_name"],
                    "date": date,
                }
            )

    for cid, bucket in buckets.items():
        bucket["profile"] = infer_profile(
            company_name=bucket["name"],
            permit_types=bucket["permit_types"],
            descriptions=bucket["descriptions"][:40],
            company_role_types=role_map.get(cid, []),
            owner_link_count=bucket["owner_links"],
            contractor_link_count=len(bucket["permits"]),
        )
        by_cap: dict[str, list[Evidence]] = defaultdict(list)
        profile = bucket["profile"]
        for obs in bucket["observations"]:
            sig = obs["sig"]
            role, role_conf, directness = attribute_observation(
                profile=profile,
                permit_type=obs["permit_type"],
                description=obs["description"],
                company_name=bucket["name"],
                general_contractor_name=obs["gc_name"],
                plumbing_contractor_name=obs["plum_name"],
                capability=sig.capability,
                signal_value=sig.signal_value,
                signal_type=sig.signal_type,
            )
            by_cap[sig.capability].append(
                Evidence(
                    capability=sig.capability,
                    signal_type=sig.signal_type,
                    signal_value=sig.signal_value,
                    weight=sig.weight,
                    permit_id=obs["permit_id"],
                    project_id=obs["project_id"],
                    evidence_date=obs["date"],
                    attribution_role=role,
                    attribution_confidence=role_conf,
                    evidence_directness=directness,
                )
            )
        bucket["by_cap"] = by_cap
    return buckets


def _dates(evidence: list[Evidence]) -> tuple[str | None, str | None]:
    values = sorted(e.evidence_date for e in evidence if e.evidence_date)
    if not values:
        return None, None
    return values[0], values[-1]


def _majority_role(evidence: list[Evidence]) -> tuple[str, float]:
    counts: dict[str, list[float]] = defaultdict(list)
    for row in evidence:
        counts[row.attribution_role].append(row.attribution_confidence)
    if not counts:
        return "unknown_role", 0.0
    role = max(counts, key=lambda k: (len(counts[k]), sum(counts[k])))
    conf = round(sum(counts[role]) / len(counts[role]), 1)
    return role, conf


def classify_companies(
    conn: sqlite3.Connection,
    *,
    model_version: str | None = None,
    min_confidence: float | None = None,
) -> dict:
    """Rebuild capability rows for ``model_version``. Returns run stats."""
    model_version = model_version or CONTRACTOR_INTEL_MODEL_VERSION
    min_confidence = (
        CONTRACTOR_INTEL_MIN_CONFIDENCE if min_confidence is None else min_confidence
    )
    seed_enrichment_registry(conn)
    now = _utcnow()

    buckets = _collect(conn)
    rows_out: list[tuple] = []
    evidence_out: list[tuple] = []

    for company_id, bucket in buckets.items():
        stored_trades: list[str] = []
        stored_classes: dict[str, str] = {}
        wrote = False
        profile = bucket["profile"]
        for capability, evidence in bucket["by_cap"].items():
            score = compute_confidence(evidence, profile=profile)
            attr_role, attr_conf = _majority_role(evidence)
            if profile.role in NON_TRADE_ROLES:
                attr_role = profile.role
                attr_conf = profile.role_confidence
            if (
                capability in TRADE_CAPABILITIES
                and attr_role in NON_TRADE_ROLES
                and profile.kind != "trade"
            ):
                score = min(score, GC_INCIDENTAL_CAP)
            if score < min_confidence:
                continue
            klass = capability_class(score, evidence, profile=profile)
            if (
                capability in TRADE_CAPABILITIES
                and attr_role in NON_TRADE_ROLES
                and profile.kind != "trade"
            ):
                klass = "incidental_project_scope"
            wrote = True
            if capability in TRADE_CAPABILITIES:
                stored_trades.append(capability)
                stored_classes[capability] = klass
            first_d, last_d = _dates(evidence)
            types = sorted({e.signal_type for e in evidence})
            permits = {e.permit_id for e in evidence if e.permit_id is not None}
            rows_out.append(
                (
                    company_id,
                    capability,
                    score,
                    len(evidence),
                    len(permits),
                    first_d,
                    last_d,
                    dominant_source(evidence),
                    json.dumps(types),
                    model_version,
                    now,
                    now,
                    attr_role,
                    attr_conf,
                    klass,
                )
            )
            for e in evidence:
                evidence_out.append(
                    (
                        company_id,
                        capability,
                        e.permit_id,
                        e.project_id,
                        e.signal_type,
                        e.signal_value,
                        e.weight,
                        e.evidence_date,
                        model_version,
                        now,
                        e.attribution_role,
                        e.attribution_confidence,
                        e.evidence_directness,
                    )
                )

        real_trades = [
            cap for cap, klass in stored_classes.items()
            if klass in {"specialist_capability", "recurring_trade_capability"}
        ]
        if len(real_trades) >= 2:
            trade_rows = [
                r for r in rows_out
                if r[0] == company_id and r[1] in real_trades
            ]
            conf = round(min(95.0, sum(r[2] for r in trade_rows) / len(trade_rows)), 1)
            firsts = [r[5] for r in trade_rows if r[5]]
            lasts = [r[6] for r in trade_rows if r[6]]
            rows_out.append(
                (
                    company_id,
                    "multi_trade",
                    conf,
                    sum(r[3] for r in trade_rows),
                    len(bucket["permits"]),
                    min(firsts) if firsts else None,
                    max(lasts) if lasts else None,
                    SIGNAL_DERIVED,
                    json.dumps([SIGNAL_DERIVED]),
                    model_version,
                    now,
                    now,
                    profile.role,
                    profile.role_confidence,
                    "recurring_trade_capability",
                )
            )
        elif not wrote and bucket["permits"]:
            rows_out.append(
                (
                    company_id,
                    "other",
                    22.0,
                    len(bucket["permits"]),
                    len(bucket["permits"]),
                    None,
                    None,
                    SIGNAL_DERIVED,
                    json.dumps([SIGNAL_DERIVED]),
                    model_version,
                    now,
                    now,
                    profile.role,
                    profile.role_confidence,
                    "insufficient_evidence",
                )
            )

    conn.execute("DELETE FROM company_capability_evidence")
    conn.execute("DELETE FROM company_capabilities")
    conn.executemany(
        """
        INSERT INTO company_capabilities (
            company_id, capability, confidence, evidence_count,
            distinct_permit_count, first_evidence_date, last_evidence_date,
            classification_source, source_types, model_version,
            created_at, updated_at, attribution_role, attribution_confidence,
            capability_class
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows_out,
    )
    conn.executemany(
        """
        INSERT INTO company_capability_evidence (
            company_id, capability, permit_id, project_id, signal_type,
            signal_value, weight, evidence_date, model_version, created_at,
            attribution_role, attribution_confidence, evidence_directness
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        evidence_out,
    )
    conn.commit()

    linked = conn.execute(
        "SELECT COUNT(*) n FROM projects WHERE contractor_company_id IS NOT NULL"
    ).fetchone()["n"]
    classified_projects = conn.execute(
        """
        SELECT COUNT(DISTINCT e.project_id) n
        FROM company_capability_evidence e
        WHERE e.model_version=? AND e.project_id IS NOT NULL
          AND e.capability != 'other'
        """,
        (model_version,),
    ).fetchone()["n"]
    return {
        "model_version": model_version,
        "companies_scanned": len(buckets),
        "capability_rows": len(rows_out),
        "evidence_rows": len(evidence_out),
        "linked_projects": linked,
        "projects_with_trade_evidence": classified_projects,
    }
