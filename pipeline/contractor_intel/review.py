"""Internal manual-review sample for contractor capability labels.

Writes 50-row slices. Heuristic judgments are starting labels for a
human pass — they are not customer-facing scores.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import (
    CONTRACTOR_INTEL_MODEL_VERSION,
    REPORTS_GENERATED_DIR,
)


def _strongest_evidence(conn: sqlite3.Connection, company_id: int, capability: str,
                        model_version: str) -> str:
    row = conn.execute(
        """
        SELECT signal_type, signal_value, weight, evidence_directness
        FROM company_capability_evidence
        WHERE company_id=? AND capability=? AND model_version=?
        ORDER BY weight DESC, id ASC
        LIMIT 1
        """,
        (company_id, capability, model_version),
    ).fetchone()
    if not row:
        return ""
    return (
        f"{row['signal_type']}={row['signal_value']} "
        f"(w={row['weight']}, {row['evidence_directness']})"
    )


def judge(row: dict) -> str:
    role = row.get("attribution_role") or ""
    klass = row.get("capability_class") or ""
    cap = row.get("capability") or ""
    name = (row.get("display_name") or "").lower()
    if role == "gc_of_record" and cap in {"fuel_gas", "plumbing"}:
        return "likely_false_positive"
    if role in {"architect_engineer_if_present", "developer", "owner_builder"} and cap in {
        "fuel_gas",
        "plumbing",
    }:
        return "likely_false_positive"
    if klass == "incidental_project_scope" and cap == "plumbing":
        if any(tok in name for tok in ("pool", "spa", "landscape", "homes", "builder")):
            return "likely_false_positive"
        return "ambiguous"
    if klass == "specialist_capability" and role == "trade_contractor":
        return "likely_correct"
    if klass == "recurring_trade_capability" and role == "trade_contractor":
        return "likely_correct"
    if klass == "incidental_project_scope":
        return "ambiguous"
    return "ambiguous"


def _slice_high(conn, capability: str, model_version: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.id AS company_id, c.display_name, cc.capability, cc.confidence,
               cc.attribution_role, cc.capability_class, cc.evidence_count
        FROM company_capabilities cc
        JOIN companies c ON c.id=cc.company_id
        WHERE cc.model_version=? AND cc.capability=? AND cc.confidence>=75
        ORDER BY cc.confidence DESC, cc.evidence_count DESC
        LIMIT ?
        """,
        (model_version, capability, limit),
    )
    return [dict(r) for r in rows]


def _slice_borderline(conn, capability: str, model_version: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.id AS company_id, c.display_name, cc.capability, cc.confidence,
               cc.attribution_role, cc.capability_class, cc.evidence_count
        FROM company_capabilities cc
        JOIN companies c ON c.id=cc.company_id
        WHERE cc.model_version=? AND cc.capability=?
          AND cc.confidence>=20 AND cc.confidence<50
        ORDER BY cc.confidence DESC, cc.evidence_count DESC
        LIMIT ?
        """,
        (model_version, capability, limit),
    )
    return [dict(r) for r in rows]


def _slice_both(conn, model_version: str, limit: int) -> list[dict]:
    rows = conn.execute(
        """
        SELECT c.id AS company_id, c.display_name,
               p.confidence AS plumbing_confidence,
               g.confidence AS fuel_gas_confidence,
               p.attribution_role AS plumbing_role,
               g.attribution_role AS fuel_gas_role,
               p.capability_class AS plumbing_class,
               g.capability_class AS fuel_gas_class
        FROM company_capabilities p
        JOIN company_capabilities g
          ON g.company_id=p.company_id AND g.capability='fuel_gas'
         AND g.model_version=p.model_version
        JOIN companies c ON c.id=p.company_id
        WHERE p.model_version=? AND p.capability='plumbing'
        ORDER BY (p.confidence + g.confidence) DESC
        LIMIT ?
        """,
        (model_version, limit),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["capability"] = "plumbing+fuel_gas"
        d["confidence"] = round(
            (float(d["plumbing_confidence"]) + float(d["fuel_gas_confidence"])) / 2.0, 1
        )
        d["attribution_role"] = d.get("fuel_gas_role") or d.get("plumbing_role")
        d["capability_class"] = d.get("fuel_gas_class") or d.get("plumbing_class")
        out.append(d)
    return out


def _decorate(conn, rows: list[dict], *, model_version: str, capability: str) -> list[dict]:
    out = []
    for row in rows:
        item = dict(row)
        cap = capability if capability != "both" else "fuel_gas"
        if capability == "both":
            gas = _strongest_evidence(conn, row["company_id"], "fuel_gas", model_version)
            plum = _strongest_evidence(conn, row["company_id"], "plumbing", model_version)
            item["strongest_evidence"] = f"gas:{gas} | plumbing:{plum}"
        else:
            item["strongest_evidence"] = _strongest_evidence(
                conn, row["company_id"], cap, model_version
            )
        item["judgment"] = judge(item)
        out.append(item)
    return out


def build_review_sample(conn: sqlite3.Connection, *,
                        model_version: str | None = None,
                        limit: int = 50) -> dict[str, list[dict]]:
    model_version = model_version or CONTRACTOR_INTEL_MODEL_VERSION
    samples = {
        "high_fuel_gas": _decorate(
            conn, _slice_high(conn, "fuel_gas", model_version, limit),
            model_version=model_version, capability="fuel_gas",
        ),
        "high_plumbing": _decorate(
            conn, _slice_high(conn, "plumbing", model_version, limit),
            model_version=model_version, capability="plumbing",
        ),
        "both": _decorate(
            conn, _slice_both(conn, model_version, limit),
            model_version=model_version, capability="both",
        ),
        "borderline_fuel_gas": _decorate(
            conn, _slice_borderline(conn, "fuel_gas", model_version, limit),
            model_version=model_version, capability="fuel_gas",
        ),
        "borderline_plumbing": _decorate(
            conn, _slice_borderline(conn, "plumbing", model_version, limit),
            model_version=model_version, capability="plumbing",
        ),
    }
    return samples


def summarize_judgments(samples: dict[str, list[dict]]) -> dict[str, dict]:
    out = {}
    for key, rows in samples.items():
        out[key] = dict(Counter(r["judgment"] for r in rows))
        out[key]["n"] = len(rows)
    return out


def write_review_sample(samples: dict[str, list[dict]]) -> Path:
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    md_path = REPORTS_GENERATED_DIR / f"contractor_intel_review_sample_{stamp}.md"
    json_path = REPORTS_GENERATED_DIR / f"contractor_intel_review_sample_{stamp}.json"
    json_path.write_text(json.dumps(samples, indent=2, default=str), encoding="utf-8")
    lines = [
        "# Contractor intelligence review sample",
        "",
        "Internal only. Heuristic judgments are a starting label for human review.",
        "",
    ]
    counts = summarize_judgments(samples)
    lines.append("## Judgment counts")
    for key, tallies in counts.items():
        lines.append(f"- {key}: {tallies}")
    lines.append("")
    for key, rows in samples.items():
        lines.append(f"## {key} (n={len(rows)})")
        for row in rows[:12]:
            lines.append(
                f"- {row.get('display_name')} | {row.get('capability')} | "
                f"conf {row.get('confidence')} | {row.get('attribution_role')} | "
                f"{row.get('capability_class')} | {row.get('judgment')} | "
                f"{row.get('strongest_evidence')}"
            )
        if len(rows) > 12:
            lines.append(f"- … {len(rows) - 12} more in {json_path.name}")
        lines.append("")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path
