"""Local daily intelligence brief. Scheduling is not enabled."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from agents.ceo.analytics.answer import answer_question
from agents.ceo.analytics.guard import AnalyticsError, open_analytics
from agents.ceo.analytics.scope import AnalyticsScope, owner_scope
from agents.ceo.analytics.tools import (
    compare_activity_periods,
    get_contact_gaps,
    get_feed_freshness,
    get_pipeline_health,
    get_ranking_changes,
    get_recent_changes,
    get_top_opportunities,
)

SCHEDULING_ENABLED = False


def schedule(*_args, **_kwargs):
    raise RuntimeError("production scheduling is not enabled")


def build_daily_brief(*, db_path: Path, as_of: str, scope: AnalyticsScope | None = None) -> dict:
    scope = scope or owner_scope()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    brief = {
        "schema": "corridoriq.ceo.daily_brief.v1",
        "generated_at": generated_at,
        "as_of": as_of,
        "execute": False,
        "scheduling_enabled": SCHEDULING_ENABLED,
        "status": "KNOWN",
        "sections": {},
    }
    try:
        conn = open_analytics(Path(db_path))
    except AnalyticsError as exc:
        brief["status"] = "UNKNOWN"
        brief["sections"] = {"error": str(exc)}
        brief["markdown"] = _markdown(brief)
        return brief
    try:
        sections = {
            "recent_changes": get_recent_changes(conn, as_of=as_of),
            "activity_1d": compare_activity_periods(conn, as_of=as_of, window_days=1),
            "activity_7d": compare_activity_periods(conn, as_of=as_of, window_days=7),
            "activity_30d": compare_activity_periods(conn, as_of=as_of, window_days=30),
            "activity_90d": compare_activity_periods(conn, as_of=as_of, window_days=90),
            "top_opportunities": get_top_opportunities(conn, as_of=as_of),
            "contact_gaps": get_contact_gaps(conn, as_of=as_of),
            "freshness": get_feed_freshness(conn, as_of=as_of),
            "pipeline": get_pipeline_health(conn, as_of=as_of),
            "ranking": get_ranking_changes(conn, as_of=as_of),
        }
    except AnalyticsError as exc:
        brief["status"] = "UNKNOWN"
        brief["sections"] = {"error": str(exc)}
        brief["markdown"] = _markdown(brief)
        return brief
    finally:
        conn.close()
    brief["sections"] = sections
    brief["display"] = section_texts(brief)
    brief["markdown"] = _markdown(brief)
    return brief


def render_markdown(brief: dict) -> str:
    return brief.get("markdown") or _markdown(brief)


def write_brief(brief: dict, directory: Path, *, history_key: str | None = None) -> dict[str, str]:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {key: value for key, value in brief.items() if key != "markdown"}
    json_text = json.dumps(payload, indent=2, default=str)
    markdown = render_markdown(brief)
    json_path = directory / "latest_daily_brief.json"
    md_path = directory / "latest_daily_brief.md"
    _atomic_text(json_path, json_text)
    _atomic_text(md_path, markdown)
    written = {"json": str(json_path), "markdown": str(md_path)}
    if history_key:
        safe = "".join(ch for ch in history_key if ch.isalnum() or ch in "._-")
        if safe:
            history = directory / "history"
            history.mkdir(parents=True, exist_ok=True)
            hist_json = history / f"{safe}.json"
            hist_md = history / f"{safe}.md"
            _atomic_text(hist_json, json_text)
            _atomic_text(hist_md, markdown)
            written["history_json"] = str(hist_json)
            written["history_markdown"] = str(hist_md)
    return written


def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


DISPLAY_SECTIONS = (
    ("executive_summary", "EXECUTIVE SUMMARY", "CALCULATION"),
    ("what_changed", "WHAT CHANGED", "CALCULATION"),
    ("top_opportunities", "TOP OPPORTUNITIES", "FACT"),
    ("why_now", "WHY NOW", "FACT"),
    ("market_movement", "MARKET MOVEMENT", "CALCULATION"),
    ("customer_supplier", "CUSTOMER / SUPPLIER SIGNALS", "FACT"),
    ("contact_gaps", "CONTACT GAPS", "CALCULATION"),
    ("pipeline_health", "PIPELINE & DATA HEALTH", "FACT"),
    ("risks", "RISKS / UNCERTAINTIES", "UNKNOWN"),
    ("priorities", "CEO PRIORITIES TODAY", "RECOMMENDATION"),
)


def section_texts(brief: dict) -> list[dict] | None:
    """Structured section copy. The admin page renders this, not the Markdown."""
    if brief.get("status") != "KNOWN":
        return None
    sections = brief["sections"]
    activity = sections["activity_30d"]
    bodies = {
        "executive_summary": _summary(sections),
        "what_changed": _changed(sections),
        "top_opportunities": _names(sections["top_opportunities"]["rows"], "account_priority_score"),
        "why_now": _why(activity),
        "market_movement": (
            f"Observed plumbing permits, last 30 days versus the prior 30: "
            f"{activity.get('current_total')} vs {activity.get('prior_total')} "
            f"(delta {activity.get('delta')})."
        ),
        "customer_supplier": (
            "Organization customer books are omitted unless an organization scope is supplied. "
            "Material requests are not in this database."
        ),
        "contact_gaps": _gaps(sections["contact_gaps"]),
        "pipeline_health": _health(sections),
        "risks": _risks(sections),
        "priorities": _priorities(activity, sections["contact_gaps"]),
    }
    return [
        {"id": key, "heading": heading, "claim_class": claim, "text": bodies[key]}
        for key, heading, claim in DISPLAY_SECTIONS
    ]


def _markdown(brief: dict) -> str:
    if brief.get("status") != "KNOWN":
        return "\n".join([
            "CORRIDORIQ CEO MORNING BRIEF",
            f"Data current through: {brief.get('as_of')}",
            "Last successful refresh: UNKNOWN",
            "",
            "EXECUTIVE SUMMARY",
            "INSUFFICIENT DATA. The brief did not invent a trend.",
            "",
        ])
    sections = brief["sections"]
    refresh = (sections.get("recent_changes") or {}).get("last_successful_refresh") or "UNKNOWN"
    blocks = section_texts(brief) or []
    lines = [
        "CORRIDORIQ CEO MORNING BRIEF",
        f"Data current through: {brief.get('as_of')}",
        f"Last successful refresh: {refresh}",
        "",
    ]
    for block in blocks:
        lines.extend([block["heading"], block["text"], ""])
    return "\n".join(lines)


def _summary(sections: dict) -> str:
    delta = sections["activity_30d"].get("delta")
    stale = [row["jurisdiction_slug"] for row in sections["freshness"]["rows"] if row.get("stale")]
    if not delta and not stale and not sections["recent_changes"]["rows"]:
        return "Nothing important changed in observed plumbing permit activity, and no stale feed was flagged."
    parts = []
    if delta:
        parts.append(f"30-day observed plumbing permits changed by {delta}.")
    new = sections["recent_changes"].get("new_permit_count")
    if new:
        parts.append(f"{new} permit row(s) were first seen after the last successful refresh.")
    if stale:
        parts.append("Stale feeds: " + ", ".join(stale) + ".")
    return " ".join(parts)


def _changed(sections: dict) -> str:
    lines = []
    for label, key in (("1 day", "activity_1d"), ("7 days", "activity_7d"), ("30 days", "activity_30d"), ("90 days", "activity_90d")):
        item = sections[key]
        lines.append(
            f"- {label}: {item.get('current_total')} observed vs {item.get('prior_total')} prior (delta {item.get('delta')})."
        )
    ranking = sections["ranking"]
    if ranking.get("claim_class") == "UNKNOWN":
        lines.append("- Ranking movement: insufficient snapshot history.")
    else:
        moved = [row for row in ranking["rows"] if int(row.get("rank_delta") or 0) != 0]
        if not moved:
            lines.append("- Ranking movement: no presentation-rank change between snapshot versions.")
        else:
            for row in moved:
                lines.append(
                    f"- {row['company_name']} presentation rank {row['prior_rank']} to {row['current_rank']}."
                )
    return "\n".join(lines)


def _names(rows: list, score_key: str) -> str:
    if not rows:
        return "No plumbing specialist or recurring-plumbing priority rows."
    return "\n".join(
        f"- {row['company_name']} (company {row['company_id']}, stored priority {row.get(score_key)})"
        for row in rows[:5]
    )


def _why(activity: dict) -> str:
    rows = [row for row in activity["rows"] if int(row.get("current_permits") or 0) > 0]
    if not rows:
        return "No company has an observed plumbing permit in the last 30 days."
    top = max(rows, key=lambda row: (int(row["current_permits"]), int(row["delta"])))
    return (
        f"FACT: {top['company_name']} has {top['current_permits']} observed permits in the current 30-day window "
        f"and {top['prior_permits']} in the prior window. "
        "INFERENCE: that may be more near-term work. It is not a dollar demand."
    )


def _gaps(gaps: dict) -> str:
    pct = gaps.get("usable_contact_pct")
    pct_text = "UNKNOWN" if pct is None else f"{pct}%"
    if not gaps["rows"]:
        return f"No high-priority contact gaps. Usable-contact coverage: {pct_text}."
    names = ", ".join(row["company_name"] for row in gaps["rows"][:5])
    return f"Usable-contact coverage among high-priority plumbing identities: {pct_text}. Gaps: {names}."


def _health(sections: dict) -> str:
    runs = sections["pipeline"]["rows"]
    if not runs:
        pipeline = "No pipeline_runs rows."
    else:
        latest = runs[0]
        pipeline = f"Latest {latest['run_type']} status {latest['status']}, records_received {latest['records_received']}."
    stale = [row["jurisdiction_slug"] for row in sections["freshness"]["rows"] if row.get("stale")]
    thin = [row["jurisdiction_slug"] for row in sections["freshness"]["rows"] if row.get("thin")]
    return pipeline + " Stale: " + (", ".join(stale) or "none") + ". Thin: " + (", ".join(thin) or "none") + "."


def _risks(sections: dict) -> str:
    lines = ["- Stored why-now can disagree with the newest permit. This brief uses observed issued dates."]
    if any(row.get("stale") for row in sections["freshness"]["rows"]):
        lines.append("- At least one feed is stale. Do not sell that city as current coverage.")
    if sections["ranking"].get("claim_class") == "UNKNOWN":
        lines.append("- Ranking movement cannot be calculated from one snapshot.")
    return "\n".join(lines)


def _priorities(activity: dict, gaps: dict) -> str:
    rows = [row for row in activity["rows"] if int(row.get("current_permits") or 0) > 0]
    if not rows:
        return "1. No observed plumbing activity to assign. Do not invent a call list."
    top = max(rows, key=lambda row: (int(row["current_permits"]), int(row["delta"])))
    lines = [f"1. Review {top['company_name']} against the permits in the current window."]
    if gaps["rows"]:
        lines.append(f"2. Do not call {gaps['rows'][0]['company_name']} from a missing contact.")
    lines.append("3. Do not turn permit counts into a material order.")
    return "\n".join(lines[:3])


def answer_for_brief_check(question: str, *, db_path: Path, as_of: str, scope: AnalyticsScope | None = None) -> dict:
    """Used by tests. Same router as ad-hoc questions."""
    return answer_question(question, db_path=db_path, as_of=as_of, scope=scope)
