"""Aggregate backtest of contractor capability classification.

Writes a short markdown summary. Does not print thousands of rows.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import (
    CONTRACTOR_INTEL_MODEL_VERSION,
    REPORTS_GENERATED_DIR,
)
from pipeline.contractor_intel.taxonomy import PHASE2_BASELINE


def _band(score: float) -> str:
    if score >= 75:
        return "high"
    if score >= 50:
        return "medium"
    if score >= 20:
        return "borderline"
    return "low"


def capability_bands(conn: sqlite3.Connection, capability: str, *,
                     model_version: str | None = None) -> dict:
    model_version = model_version or CONTRACTOR_INTEL_MODEL_VERSION
    row = conn.execute(
        """
        SELECT COUNT(*) total,
               COALESCE(SUM(CASE WHEN confidence >= 75 THEN 1 ELSE 0 END), 0) high,
               COALESCE(SUM(CASE WHEN confidence >= 50 AND confidence < 75 THEN 1 ELSE 0 END), 0) medium,
               COALESCE(SUM(CASE WHEN confidence >= 20 AND confidence < 50 THEN 1 ELSE 0 END), 0) borderline
        FROM company_capabilities
        WHERE model_version=? AND capability=?
        """,
        (model_version, capability),
    ).fetchone()
    high_gc = conn.execute(
        """
        SELECT COUNT(*) FROM company_capabilities
        WHERE model_version=? AND capability=? AND confidence >= 75
          AND attribution_role = 'gc_of_record'
        """,
        (model_version, capability),
    ).fetchone()[0]
    specialists = conn.execute(
        """
        SELECT COUNT(*) FROM company_capabilities
        WHERE model_version=? AND capability=?
          AND capability_class = 'specialist_capability'
        """,
        (model_version, capability),
    ).fetchone()[0]
    return {
        "total": int(row["total"] or 0),
        "high": int(row["high"] or 0),
        "medium": int(row["medium"] or 0),
        "borderline": int(row["borderline"] or 0),
        "high_gc": int(high_gc or 0),
        "specialists": int(specialists or 0),
    }


def named_company_rows(conn: sqlite3.Connection, names: list[str], *,
                       model_version: str | None = None) -> dict[str, list[dict]]:
    model_version = model_version or CONTRACTOR_INTEL_MODEL_VERSION
    out: dict[str, list[dict]] = {}
    for name in names:
        rows = conn.execute(
            """
            SELECT cc.capability, cc.confidence, cc.attribution_role,
                   cc.capability_class, cc.evidence_count
            FROM company_capabilities cc
            JOIN companies c ON c.id=cc.company_id
            WHERE cc.model_version=? AND c.display_name=?
            ORDER BY cc.capability
            """,
            (model_version, name),
        )
        out[name] = [dict(r) for r in rows]
    return out


def summarize(conn: sqlite3.Connection, *, model_version: str | None = None) -> dict:
    model_version = model_version or CONTRACTOR_INTEL_MODEL_VERSION

    def n(sql: str, params=()) -> int:
        return int(conn.execute(sql, params).fetchone()[0])

    plumbing = capability_bands(conn, "plumbing", model_version=model_version)
    fuel_gas = capability_bands(conn, "fuel_gas", model_version=model_version)
    both = n(
        """
        SELECT COUNT(*) FROM company_capabilities a
        JOIN company_capabilities b
          ON a.company_id=b.company_id AND b.capability='fuel_gas' AND b.model_version=a.model_version
        WHERE a.model_version=? AND a.capability='plumbing'
        """,
        (model_version,),
    )
    bands = Counter()
    for row in conn.execute(
        "SELECT confidence FROM company_capabilities WHERE model_version=?",
        (model_version,),
    ):
        bands[_band(row[0])] += 1

    gas_permits = n(
        """
        SELECT COUNT(DISTINCT permit_id) FROM company_capability_evidence
        WHERE model_version=? AND capability='fuel_gas' AND permit_id IS NOT NULL
        """,
        (model_version,),
    )
    phrases = list(
        conn.execute(
            """
            SELECT signal_value, COUNT(*) n
            FROM company_capability_evidence
            WHERE model_version=? AND capability='fuel_gas' AND signal_type='description_phrase'
            GROUP BY signal_value ORDER BY n DESC LIMIT 15
            """,
            (model_version,),
        )
    )
    linked = n("SELECT COUNT(*) FROM projects WHERE contractor_company_id IS NOT NULL")
    useful = n(
        """
        SELECT COUNT(DISTINCT p.id)
        FROM projects p
        JOIN company_capabilities cc ON cc.company_id=p.contractor_company_id
        WHERE cc.model_version=? AND cc.capability NOT IN ('other')
          AND COALESCE(cc.capability_class, '') NOT IN ('incidental_project_scope', 'insufficient_evidence')
        """,
        (model_version,),
    )
    useful_any = n(
        """
        SELECT COUNT(DISTINCT p.id)
        FROM projects p
        JOIN company_capabilities cc ON cc.company_id=p.contractor_company_id
        WHERE cc.model_version=? AND cc.capability NOT IN ('other')
        """,
        (model_version,),
    )
    return {
        "model_version": model_version,
        "plumbing": plumbing,
        "fuel_gas": fuel_gas,
        "plumbing_companies": plumbing["total"],
        "fuel_gas_companies": fuel_gas["total"],
        "both_companies": both,
        "confidence_bands": dict(bands),
        "gas_evidence_permits": gas_permits,
        "top_gas_phrases": [(r[0], r[1]) for r in phrases],
        "linked_projects": linked,
        "projects_with_useful_classification": useful,
        "projects_with_any_trade_class": useful_any,
        "useful_pct": round(100.0 * useful / linked, 1) if linked else 0.0,
        "useful_any_pct": round(100.0 * useful_any / linked, 1) if linked else 0.0,
        "phase2_baseline": PHASE2_BASELINE,
        "capability_counts": [
            (r[0], r[1])
            for r in conn.execute(
                """
                SELECT capability, COUNT(*) n FROM company_capabilities
                WHERE model_version=? GROUP BY capability ORDER BY n DESC
                """,
                (model_version,),
            )
        ],
        "named": named_company_rows(
            conn,
            [
                "SHEA HOMES",
                "LENNAR ARIZONA CONS. CO",
                "HIGLEY HOMES, LLC",
                "CREATIVE ENVIRONMENTS",
                "PEGASUS POOLS",
            ],
            model_version=model_version,
        ),
    }


def example_rows(conn: sqlite3.Connection, capability: str, *,
                 min_conf: float, max_conf: float, limit: int = 5,
                 model_version: str | None = None) -> list[dict]:
    model_version = model_version or CONTRACTOR_INTEL_MODEL_VERSION
    rows = conn.execute(
        """
        SELECT c.id, c.display_name, cc.confidence, cc.evidence_count,
               cc.distinct_permit_count, cc.classification_source,
               cc.attribution_role, cc.capability_class
        FROM company_capabilities cc
        JOIN companies c ON c.id=cc.company_id
        WHERE cc.model_version=? AND cc.capability=?
          AND cc.confidence>=? AND cc.confidence<?
        ORDER BY cc.confidence DESC, cc.evidence_count DESC
        LIMIT ?
        """,
        (model_version, capability, min_conf, max_conf, limit),
    )
    return [dict(r) for r in rows]


def false_positive_candidates(conn: sqlite3.Connection, *,
                              model_version: str | None = None,
                              limit: int = 8) -> list[dict]:
    """Gas-station / gasoline descriptions that still received fuel_gas."""
    model_version = model_version or CONTRACTOR_INTEL_MODEL_VERSION
    rows = conn.execute(
        """
        SELECT DISTINCT c.display_name, cc.confidence, p.permit_type, p.description
        FROM company_capabilities cc
        JOIN companies c ON c.id=cc.company_id
        JOIN company_capability_evidence e
          ON e.company_id=cc.company_id AND e.capability='fuel_gas'
         AND e.model_version=cc.model_version
        JOIN permits p ON p.id=e.permit_id
        WHERE cc.model_version=? AND cc.capability='fuel_gas'
          AND (
            lower(p.description) LIKE '%gas station%'
            OR lower(p.description) LIKE '%gasoline%'
            OR lower(c.display_name) LIKE '%gas station%'
          )
        LIMIT ?
        """,
        (model_version, limit),
    )
    return [dict(r) for r in rows]


def _fmt_named(named: dict) -> list[str]:
    lines = []
    for name, rows in named.items():
        if not rows:
            lines.append(f"- {name}: no capability rows")
            continue
        bits = [
            f"{r['capability']} {r['confidence']} ({r.get('attribution_role')}/"
            f"{r.get('capability_class')})"
            for r in rows
        ]
        lines.append(f"- {name}: " + "; ".join(bits))
    return lines


def write_report(conn: sqlite3.Connection, stats: dict, *,
                 high: list, borderline: list, fps: list) -> Path:
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = REPORTS_GENERATED_DIR / f"contractor_intel_backtest_{stamp}.md"
    p2 = stats.get("phase2_baseline") or PHASE2_BASELINE
    fg, pl = stats["fuel_gas"], stats["plumbing"]
    lines = [
        f"# Contractor intelligence backtest ({stats['model_version']})",
        "",
        "Internal only. Do not publish source families or scoring methodology.",
        "",
        "## BEFORE (contractor-intel-v1) vs AFTER",
        "",
        "### Fuel gas",
        f"- total: {p2['fuel_gas']['total']} → {fg['total']}",
        f"- high: {p2['fuel_gas']['high']} → {fg['high']}",
        f"- medium: {p2['fuel_gas']['medium']} → {fg['medium']}",
        f"- borderline: {p2['fuel_gas']['borderline']} → {fg['borderline']}",
        f"- high-confidence GCs: (v1 name-heuristic builders) → {fg['high_gc']}",
        f"- likely specialists: → {fg['specialists']}",
        "",
        "### Plumbing",
        f"- total: {p2['plumbing']['total']} → {pl['total']}",
        f"- high: {p2['plumbing']['high']} → {pl['high']}",
        f"- medium: {p2['plumbing']['medium']} → {pl['medium']}",
        f"- borderline: {p2['plumbing']['borderline']} → {pl['borderline']}",
        f"- high-confidence GCs: → {pl['high_gc']}",
        f"- likely specialists: → {pl['specialists']}",
        "",
        f"- Both plumbing + fuel_gas: {p2['both']} → {stats['both_companies']}",
        f"- Useful coverage (non-incidental): {stats['projects_with_useful_classification']}"
        f" / {stats['linked_projects']} ({stats['useful_pct']}%)",
        f"- Any trade class coverage: {stats['projects_with_any_trade_class']}"
        f" ({stats['useful_any_pct']}%)",
        f"- Gas-evidence permits: {stats['gas_evidence_permits']}",
        "",
        "## Named validation",
        *_fmt_named(stats.get("named") or {}),
        "",
        "## Overall confidence bands",
        *[f"- {k}: {v}" for k, v in sorted(stats["confidence_bands"].items())],
        "",
        "## Capability counts",
        *[f"- {cap}: {n}" for cap, n in stats["capability_counts"]],
        "",
        "## Top gas description phrases",
        *[f"- {p}: {n}" for p, n in stats["top_gas_phrases"]],
        "",
        "## High-confidence fuel_gas examples",
        *[
            f"- {r['display_name']} (conf {r['confidence']}, n={r['evidence_count']},"
            f" {r.get('attribution_role')}/{r.get('capability_class')})"
            for r in high
        ],
        "",
        "## Borderline fuel_gas examples",
        *[
            f"- {r['display_name']} (conf {r['confidence']}, n={r['evidence_count']},"
            f" {r.get('attribution_role')}/{r.get('capability_class')})"
            for r in borderline
        ],
        "",
        "## Possible false positives (gas station / gasoline still classified)",
        *(
            [f"- {r['display_name']} / {r.get('permit_type')} / {(r.get('description') or '')[:80]}"
             for r in fps]
            or ["- none found"]
        ),
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
