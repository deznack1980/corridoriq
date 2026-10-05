"""Turn a business question into structured claims. Wording is optional; facts are not."""

from __future__ import annotations

from pathlib import Path

from agents.ceo.analytics.guard import AnalyticsError, open_analytics
from agents.ceo.analytics.limits import MAX_TOOL_CALLS
from agents.ceo.analytics.router import route
from agents.ceo.analytics.scope import AnalyticsScope, owner_scope
from agents.ceo.analytics.tools import call_tool
from agents.framework.provider import ModelDisabled, ModelProvider, narration_enabled


def answer_question(
    question: str,
    *,
    db_path: Path | None,
    as_of: str,
    scope: AnalyticsScope | None = None,
) -> dict:
    scope = scope or owner_scope()
    plan = route(question)
    base = {
        "question": question,
        "as_of": as_of,
        "execute": False,
        "scope": {"kind": scope.kind, "organization_id": scope.organization_id},
        "tools": [],
        "claims": [],
        "model": "not_used",
    }
    if plan.status == "UNSUPPORTED":
        base["status"] = "UNSUPPORTED"
        base["claims"] = [{"class": "UNKNOWN", "text": plan.reason, "evidence": {}}]
        return base
    if db_path is None:
        base["status"] = "UNKNOWN"
        base["claims"] = [{"class": "UNKNOWN", "text": "database unavailable", "evidence": {}}]
        return base
    try:
        conn = open_analytics(Path(db_path))
    except AnalyticsError as exc:
        base["status"] = "UNKNOWN"
        base["claims"] = [{"class": "UNKNOWN", "text": str(exc), "evidence": {}}]
        return base
    results = []
    try:
        for name, kwargs in plan.steps[:MAX_TOOL_CALLS]:
            try:
                result = call_tool(conn, name, scope, as_of=as_of, **kwargs)
            except AnalyticsError as exc:
                base["status"] = "UNKNOWN"
                base["claims"] = [{"class": "UNKNOWN", "text": str(exc), "evidence": {"tool": name}}]
                base["tools"] = [item.get("tool") for item in results]
                return base
            if not isinstance(result, dict) or "rows" not in result:
                base["status"] = "UNKNOWN"
                base["claims"] = [{"class": "UNKNOWN", "text": "malformed tool result", "evidence": {"tool": name}}]
                return base
            results.append(result)
    finally:
        conn.close()
    claims = _claims(results)
    base["status"] = "KNOWN" if claims else "UNKNOWN"
    base["tools"] = [item["tool"] for item in results]
    base["results"] = results
    base["claims"] = claims
    base["model"] = _maybe_narrate(base)
    return base


def _claims(results: list[dict]) -> list[dict]:
    claims = []
    for result in results:
        if result.get("claim_class") == "UNKNOWN" or result.get("reason"):
            claims.append({
                "class": "UNKNOWN",
                "text": result.get("reason") or result.get("definition"),
                "evidence": {"tool": result.get("tool"), **{k: result[k] for k in result if k in {"last_successful_refresh", "versions", "organization_id"}}},
            })
        claims.append({
            "class": result.get("claim_class") or "FACT",
            "text": result.get("definition"),
            "evidence": {
                "tool": result.get("tool"),
                "row_count": result.get("row_count"),
                "rows": result.get("rows"),
                "window": result.get("window") or result.get("current_window"),
                "current_total": result.get("current_total"),
                "prior_total": result.get("prior_total"),
                "delta": result.get("delta"),
                "usable_contact_pct": result.get("usable_contact_pct"),
                "high_priority": result.get("high_priority"),
                "with_usable_contact": result.get("with_usable_contact"),
                "new_permit_count": result.get("new_permit_count"),
                "last_successful_refresh": result.get("last_successful_refresh"),
            },
        })
    if any(item.get("tool") == "compare_activity_periods" and (item.get("delta") or 0) > 0 for item in results):
        claims.append({
            "class": "INFERENCE",
            "text": "An increase in observed permits may indicate more near-term work. It is not a material-dollar demand.",
            "evidence": {"tool": "compare_activity_periods"},
        })
    if results:
        top = _focus_name(results)
        if top:
            claims.append({
                "class": "RECOMMENDATION",
                "text": f"Review {top} first. Read the permit text before treating stored why-now as the job.",
                "evidence": {"company_name": top},
            })
    return claims


def _focus_name(results: list[dict]) -> str | None:
    for result in results:
        if result.get("tool") != "compare_activity_periods":
            continue
        rows = [row for row in result.get("rows") or [] if int(row.get("current_permits") or 0) > 0]
        if not rows:
            return None
        rows.sort(key=lambda row: (-int(row["current_permits"]), -int(row["delta"]), row["company_id"]))
        return rows[0].get("company_name")
    return None


def _maybe_narrate(payload: dict) -> str:
    if not narration_enabled():
        return "not_used"
    try:
        ModelProvider().complete(
            [{"role": "user", "content": "Summarize only the supplied claims."}],
            purpose="analytics-narration",
        )
    except (ModelDisabled, Exception):
        return "unavailable"
    return "used"
