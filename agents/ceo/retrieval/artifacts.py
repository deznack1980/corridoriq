"""Parse the latest generated commercial artifacts. Numbers come from files."""

from __future__ import annotations

import json
import re
from pathlib import Path

from agents.framework.provenance import VERIFIED_SYSTEM_STATE, metric, unknown

_STAMP = re.compile(r"_(\d{8}T\d{6}Z)")
_TARGET = re.compile(r"target:\s*>=\s*(\d+(?:\.\d+)?)%", re.IGNORECASE)


def _stamp(path: Path) -> str | None:
    match = _STAMP.search(path.name)
    if not match:
        return None
    raw = match.group(1)
    return (
        f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T"
        f"{raw[9:11]}:{raw[11:13]}:{raw[13:15]}Z"
    )


def _operating_sales_trial(directory: Path) -> tuple[Path | None, list[str]]:
    """Newest sales-trial JSON that looks like an operating run.

    Unit tests write a later file whose project guardrail is 0. That file is
    not the warehouse state when an earlier artifact counted real projects.
    """
    if not directory.exists():
        return None, []
    files = sorted(directory.glob("sales_trial_*.json"))
    viable: list[Path] = []
    for path in files:
        projects = _guard_projects(path)
        if projects is not None and projects > 0:
            viable.append(path)
    if viable:
        chosen = viable[-1]
        skipped = [path.name for path in files if path.name > chosen.name]
        return chosen, skipped
    return (files[-1] if files else None), []


def _guard_projects(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    value = ((payload.get("guardrails") or {}).get("after") or {}).get("projects")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _latest(directory: Path, pattern: str) -> Path | None:
    if not directory.exists():
        return None
    files = sorted(directory.glob(pattern))
    return files[-1] if files else None


def _section(text: str, start: str, end: str) -> str:
    begin = text.find(start)
    if begin < 0:
        return ""
    finish = text.find(end, begin + len(start))
    if finish < 0:
        return text[begin:]
    return text[begin:finish]


def _trial_names(markdown: str) -> list[str]:
    body = _section(markdown, "## J.", "## K.")
    names = []
    for line in body.splitlines():
        if line.startswith("### "):
            names.append(line[4:].strip())
    return names


def _flag(text: str, phrase: str, value: bool) -> dict:
    if phrase.lower() in text.lower():
        return metric(
            value,
            source="sales-trial-report",
            category=VERIFIED_SYSTEM_STATE,
            note=phrase,
        )
    return unknown(f"report does not state: {phrase}", source="sales-trial-report")


def load_sales_artifacts(reports_dir: Path) -> dict:
    """Cohort metrics from the newest sales_trial JSON, plus nearby reports."""
    path, skipped = _operating_sales_trial(reports_dir)
    if path is None:
        return {"present": False, "metrics": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"present": False, "metrics": {}}
    markdown = ""
    companion = path.with_suffix(".md")
    if companion.exists():
        markdown = companion.read_text(encoding="utf-8", errors="replace")
    timestamp = _stamp(path)
    source = path.name
    after = payload.get("after_kpis") or {}
    pct = after.get("pct") or {}
    counts = after.get("counts") or {}
    cohort_n = after.get("n")
    target = None
    found = _TARGET.search(markdown)
    if found:
        target = float(found.group(1))

    def pct_metric(key: str, count_key: str | None = None) -> dict:
        if key not in pct:
            return unknown(f"{key} not in {source}", source=source)
        extra = {"n": cohort_n, "target": target}
        if count_key and count_key in counts:
            extra["count"] = counts[count_key]
        return metric(
            pct[key],
            source=source,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=timestamp,
            **extra,
        )

    names = _trial_names(markdown)
    trial_metric = (
        metric(
            names,
            source=companion.name if companion.exists() else source,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=timestamp,
        )
        if names
        else unknown("trial account names not in the sales-trial markdown", source=source)
    )
    callability = payload.get("callability_counts")
    if isinstance(callability, dict) and callability:
        call_metric = metric(
            {str(k): int(v) for k, v in callability.items()},
            source=source,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=timestamp,
        )
    else:
        call_metric = unknown("callability_counts missing", source=source)
    guard = (payload.get("guardrails") or {}).get("after") or {}
    outcomes = payload.get("outcomes_design") or {}
    if str(outcomes.get("status", "")).upper() == "DESIGN_ONLY":
        field_outcomes = metric(
            0,
            source=source,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=timestamp,
            note="call-outcome schema is design-only in this artifact",
        )
    else:
        field_outcomes = unknown(
            "artifact does not say whether call outcomes were recorded",
            source=source,
        )
    section_j = _section(markdown, "## J.", "## K.")
    if names and section_j:
        in_crm = sum(
            1
            for block in section_j.split("### ")[1:]
            if "CRM:" in block and "CRM: none" not in block
        )
        trial_crm = metric(
            in_crm,
            source=companion.name if companion.exists() else source,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=timestamp,
        )
    else:
        trial_crm = unknown("trial CRM lines not in the report", source=source)

    lanes = _latest(reports_dir, "sales_lanes_*.md")
    lane_text = lanes.read_text(encoding="utf-8", errors="replace") if lanes else ""
    if "presentation layer only" in lane_text.lower():
        separated = metric(
            True,
            source=lanes.name,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=_stamp(lanes) if lanes else timestamp,
            note="sales lanes are a presentation layer",
        )
    else:
        separated = unknown(
            "no sales-lane report stating presentation-only separation",
            source="sales-lanes-report",
        )
    roc = _latest(reports_dir, "roc_identity_*.md")
    roc_text = roc.read_text(encoding="utf-8", errors="replace") if roc else ""
    if "shadow validation only" in roc_text.lower():
        roc_role = metric(
            "identity_validation_not_ranking",
            source=roc.name,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=_stamp(roc) if roc else timestamp,
        )
    else:
        roc_role = unknown("no ROC validation report in the reports directory")

    def guard_count(key: str) -> dict:
        if key not in guard:
            return unknown(f"{key} not in sales-trial guardrail", source=source)
        return metric(
            guard[key],
            source=source,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=timestamp,
            note="sales-trial guardrail",
        )

    result = {
        "present": True,
        "source": source,
        "timestamp": timestamp,
        "metrics": {
            "actionable_public_contact_pct": pct_metric("actionable", "actionable"),
            "verified_contact_pct": pct_metric("verified", "verified"),
            "business_phone_pct": pct_metric("has_phone", "has_phone"),
            "business_email_pct": pct_metric("has_email", "has_email"),
            "website_pct": pct_metric("has_website", "has_website"),
            "named_decision_maker_pct": pct_metric("has_named", "has_named"),
            "purchasing_ops_count": (
                metric(
                    counts["has_purchasing_ops"],
                    source=source,
                    category=VERIFIED_SYSTEM_STATE,
                    timestamp=timestamp,
                    n=cohort_n,
                )
                if "has_purchasing_ops" in counts
                else unknown("purchasing contact count missing", source=source)
            ),
            "no_actionable_contact_count": (
                metric(
                    counts["no_contact"],
                    source=source,
                    category=VERIFIED_SYSTEM_STATE,
                    timestamp=timestamp,
                    n=cohort_n,
                )
                if "no_contact" in counts
                else unknown("no-contact count missing", source=source)
            ),
            "cohort_size": (
                metric(cohort_n, source=source, category=VERIFIED_SYSTEM_STATE, timestamp=timestamp)
                if cohort_n is not None
                else unknown("cohort size missing", source=source)
            ),
            "callability": call_metric,
            "learning_trial_accounts": trial_metric,
            "recorded_field_outcomes": field_outcomes,
            "trial_accounts_with_crm": trial_crm,
            "fulfillment_implemented": _flag(
                markdown, "Fulfillment is not implemented", False
            ),
            "dashboard_cutover_authorized": _flag(
                markdown, "Do not cut over the customer dashboard", False
            ),
            "plumbing_lane_separated": separated,
            "roc_role": roc_role,
            "guard_projects": guard_count("projects"),
            "guard_relevance_rows": guard_count("relevance_rows"),
            "guard_priority_rows": guard_count("priority_rows"),
            "guard_roc_enabled": guard_count("roc_enabled"),
            "guard_crm_relationships": guard_count("crm_relationships"),
            "guard_merged_companies": guard_count("merged_companies"),
        },
    }
    if skipped:
        result["metrics"]["ignored_newer_reports"] = metric(
            skipped,
            source=source,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=timestamp,
            note="zero project guardrail; not used as the operating cohort",
        )
    return result
