"""Trust-gated opportunity builder shared by the CEO Morning Operator and the
sales portal. Read-only: every query is a SELECT.

Steps: recent attributed permits -> per-permit scope -> one candidate permit
per company -> trust gates -> small queue. The queue is never padded.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import datetime, timedelta

from pipeline.trust import contract as C
from pipeline.trust import gates as G
from pipeline.trust import scope as S
from pipeline.trust.dates import days_between, parse_date, parse_ts
from pipeline.trust.evidence import (
    attach_name_peers,
    company_context,
    name_peer_index,
    recent_permits,
)
from pipeline.trust.health import collect_health, source_for

_PLUMBING_STORED = {"plumbing_fixture", "plumbing_service", "water_heater", "commercial_plumbing"}
_NON_PLUMBING_SCOPES = {S.FUEL_GAS, S.PROPANE, S.FIRE_LINE, S.CIVIL_WET, S.MECHANICAL}

# ---------------------------------------------------------------- helpers


def _activity(permit: dict) -> tuple[str | None, str | None]:
    """Observed activity day and which field it came from."""
    for field in ("issued_date", "filed_date"):
        day = parse_date(permit.get(field))
        if day is not None:
            return day.isoformat(), field
    return None, None


def _clean(text: str | None, limit: int = 220) -> str:
    if not text:
        return ""
    t = str(text).replace("�", " ").replace("—", "-").replace("–", "-")
    t = " ".join(t.split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


# ---------------------------------------------------------------- candidates


def _classified(permits: list[dict], as_of: datetime) -> list[dict]:
    out = []
    for p in permits:
        day, basis = _activity(p)
        if day is None or day > as_of.date().isoformat():
            continue
        sc = S.classify_permit(
            permit_type=p.get("permit_type"),
            permit_subtype=p.get("permit_subtype"),
            description=p.get("description"),
            project_description=p.get("project_description"),
        )
        item = dict(p)
        item["activity_date"] = day
        item["activity_basis"] = basis
        item["activity_days"] = days_between(parse_date(day), as_of)
        item["scope"] = sc
        out.append(item)
    return out


def _pick(wet: list[dict]) -> dict | None:
    """Newest callable-lane permit inside the call window first, then others."""

    def tier(p):
        lane = S.primary_lane(p["scope"])
        inside = p["activity_days"] is not None and p["activity_days"] <= G.CALL_WINDOW_DAYS
        callable_lane = lane in S.CALLABLE_LANES
        return (0 if inside and callable_lane else 1 if inside else 2 if callable_lane else 3)

    if not wet:
        return None
    return sorted(
        wet,
        key=lambda p: (
            tier(p),
            -(parse_date(p["activity_date"]).toordinal()),
            S.LANE_RANK.get(S.primary_lane(p["scope"]), 9),
            -int(p["permit_id"]),
        ),
    )[0]


def _why_now(p: dict, lane: str | None, others: list[dict]) -> str:
    sc = p["scope"]
    lane_scope = S.scope_for_lane(sc, lane) or sc["primary"]
    label = S.LABELS[lane_scope]
    date_word = "issued" if p["activity_basis"] == "issued_date" else "filed"
    text = (
        f"{label[:1].upper() + label[1:]} permit {p.get('permit_number')} ({p.get('jurisdiction')}) "
        f"{date_word} {p['activity_date']}, {p['activity_days']} days ago"
    )
    snippet = _clean(p.get("description") or p.get("project_description"), 140)
    if snippet:
        text += f': "{snippet}"'
    text += "."
    if lane_scope in {S.FUEL_GAS, S.PROPANE}:
        text += " This is fuel-gas work, not plumbing service."
    elif lane_scope == S.FIRE_LINE:
        text += " This is fire-line work, not general plumbing."
    elif lane_scope == S.UNKNOWN_WET:
        text += " The description does not state the plumbing scope."
    if others:
        kinds = Counter(S.LABELS[o["scope"]["primary"]] for o in others)
        summary = ", ".join(f"{n} {k}" for k, n in kinds.most_common(3))
        text += f" Other wet-side permits in the last {G.REVIEW_WINDOW_DAYS} days: {summary}."
    return text


def _why_trade(p: dict, lane: str | None) -> str:
    sc = p["scope"]
    lane_scope = S.scope_for_lane(sc, lane) or sc["primary"]
    terms = sc["matched_terms"].get(lane_scope) or []
    basis = sc["basis"].get(lane_scope)
    if basis == "description":
        return f"Permit text contains {', '.join(repr(t) for t in terms)} -> {S.LABELS[lane_scope]} ({lane})."
    if basis == "permit_type":
        return f"Only the permit type supports this ({'; '.join(terms)}); the description does not state scope."
    return "No trade evidence on this permit."


def _stored_compare(ctx: dict, p: dict, lane_scope: str) -> dict:
    pri = ctx.get("priority") or {}
    disagreements = []
    stored_date = pri.get("most_recent_relevant_date")
    if stored_date and stored_date[:10] < p["activity_date"]:
        disagreements.append(
            f"stored why-now latest date {stored_date[:10]} is older than observed activity {p['activity_date']}"
        )
    stored_primary = pri.get("primary_demand_category")
    if stored_primary in _PLUMBING_STORED and lane_scope in _NON_PLUMBING_SCOPES:
        disagreements.append(
            f"stored primary demand '{stored_primary}' describes account history; this permit is {S.LABELS[lane_scope]}"
        )
    if pri.get("generated_at") and parse_ts(pri["generated_at"]) and p.get("first_seen_at"):
        seen = parse_ts(p["first_seen_at"])
        if seen and seen > parse_ts(pri["generated_at"]):
            disagreements.append("permit was ingested after the stored priority row was generated")
    return {
        "text": pri.get("why_now"),
        "most_recent_relevant_date": stored_date,
        "primary_demand_category": stored_primary,
        "generated_at": pri.get("generated_at"),
        "disagreements": disagreements,
    }


def _recommend(action: str, card: dict) -> str:
    obs = card["observed"]
    ref = f"permit {obs['permit_number']} ({obs['jurisdiction']})"
    reasons = "; ".join(b["reason"] for b in card["gate_result"]["blockers"])
    if action == C.CALL_NOW:
        return f"Call the verified business line on file and reference {ref}. Record the outcome."
    if action == C.EMAIL:
        return f"Email the verified business address on file and reference {ref}. Record the outcome."
    if action == C.FOLLOW_UP:
        return f"Follow up on the prior conversation; {ref} is the newest evidence. Record the outcome."
    if action == C.VERIFY_CONTRACTOR:
        return f"Confirm this company record is the contractor on {ref} before any contact. {reasons}."
    if action == C.VERIFY_PROJECT_ROLE:
        return f"Confirm whether this company performs the {card['derived']['scope_label']} work on {ref}. {reasons}."
    if action == C.RESEARCH:
        return f"Research only; not ready for outreach. {reasons}."
    if action == C.HOLD:
        return f"Hold. {reasons}."
    return f"Do not contact. {reasons}."


def _verification_list(result: dict) -> list[str]:
    items = []
    names = {b["gate"] for b in result["blockers"]}
    if "contractor_role" in names or "ROLE_FROM_ACCOUNT_HISTORY" in result["flags"]:
        items.append("Confirm the company performs the wet-side work on this permit (role is not stated on the permit).")
    if "identity" in names or "DUPLICATE_NAME_PEER_UNMERGED" in result["flags"]:
        items.append("Confirm which company record is the real contractor; do not merge records.")
    if "contact" in names:
        items.append("Find a verified public business channel for this company row.")
    if "source_fresh" in names or "refresh_recent" in names:
        items.append("Confirm the source is current before relying on recency.")
    items.append("Confirm scope is still open; permit status is not proof of material need.")
    return items


def build_card(ctx: dict, wet: list[dict], health: dict, prior: dict | None) -> dict | None:
    p = _pick(wet)
    if p is None:
        return None
    lane = S.primary_lane(p["scope"])
    lane_scope = S.scope_for_lane(p["scope"], lane) or p["scope"]["primary"]
    others = [o for o in wet if o["permit_id"] != p["permit_id"]]
    source = source_for(health, p.get("jurisdiction"))
    role = G.role_for(ctx, p, lane)
    identity = G.identity_state(ctx)
    contact = G.contact_state(ctx)
    result = G.evaluate(
        ctx=ctx,
        lane=lane,
        scope={**p["scope"], "explicit": p["scope"]["basis"].get(lane_scope) == "description"},
        activity_days=p["activity_days"],
        source=source,
        refresh_state=health["refresh"]["state"],
        role=role,
        identity=identity,
        contact=contact,
        prior_outcome=prior,
    )
    company = ctx.get("company") or {}
    pri = ctx.get("priority") or {}
    cid = int(p["company_id"])
    card = {
        "opportunity_id": C.opportunity_id(cid, int(p["permit_id"])),
        "action": result["action"],
        "confidence": result["confidence"],
        "company": {
            "company_id": cid,
            "display_name": company.get("display_name"),
            "canonical_name": identity.get("canonical"),
            "identity": identity,
        },
        "account_priority_score": pri.get("account_priority_score"),
        "trade_identity": pri.get("trade_identity"),
        "book_lanes": G.book_lanes(ctx),
        "observed": {
            "permit_id": int(p["permit_id"]),
            "permit_number": p.get("permit_number"),
            "project_id": p.get("project_id"),
            "jurisdiction": p.get("jurisdiction"),
            "address": p.get("job_address"),
            "city": p.get("city"),
            "zip": p.get("zip"),
            "permit_type": p.get("permit_type"),
            "permit_status": p.get("status"),
            "description": _clean(p.get("description") or p.get("project_description"), 240),
            "activity_date": p["activity_date"],
            "activity_date_field": p["activity_basis"],
            "general_contractor_on_permit": p.get("general_contractor_name"),
            "plumbing_contractor_on_permit": p.get("plumbing_contractor_name"),
            "first_seen_at": p.get("first_seen_at"),
            "permit_url": p.get("permit_url"),
        },
        "derived": {
            "scope": lane_scope,
            "scope_label": S.LABELS[lane_scope],
            "all_scope_labels": p["scope"]["labels"],
            "matched_terms": p["scope"]["matched_terms"],
            "lane": lane,
            "activity_days": p["activity_days"],
            "role": role,
            "pool_context": p["scope"]["pool_context"],
            "pipeline_opportunity_date": p.get("opportunity_date"),
            "other_recent_wet_permits": [
                {
                    "permit_id": int(o["permit_id"]),
                    "permit_number": o.get("permit_number"),
                    "activity_date": o["activity_date"],
                    "scope": o["scope"]["primary_label"],
                }
                for o in sorted(others, key=lambda o: o["activity_date"], reverse=True)[:4]
            ],
        },
        "not_observed": list(C.NOT_OBSERVED_ITEMS),
        "why_now": _why_now(p, lane, others),
        "why_this_trade": _why_trade(p, lane),
        "contact": contact,
        "source": {
            "jurisdiction": source.get("jurisdiction"),
            "freshness": source.get("freshness"),
            "newest_record_date": source.get("newest_record_date"),
            "run_health": source.get("run_health"),
            "warnings": source.get("warnings"),
        },
        "gate_result": result,
        "stored_why_now": _stored_compare(ctx, p, lane_scope),
        "prior_outcome": prior,
        "evidence_refs": [
            f"permits.id={int(p['permit_id'])}",
            f"projects.id={p.get('project_id')} (contractor_company_id={cid})",
        ]
        + ([f"company_contact_channels.id={contact['channel_id']}"] if contact.get("channel_id") else [])
        + [f"company_sales_lanes company_id={cid} lanes={G.book_lanes(ctx)}"],
    }
    card["recommended_action"] = _recommend(result["action"], card)
    card["human_verification_required"] = _verification_list(result)
    card["uncertainty"] = result["flags"] + [b["reason"] for b in result["blockers"]]
    return card


# ---------------------------------------------------------------- run


def _order_queue(cards: list[dict]) -> list[dict]:
    return sorted(
        cards,
        key=lambda c: (
            C.CONFIDENCE_RANK[c["confidence"]],
            -parse_date(c["observed"]["activity_date"]).toordinal(),
            -(c.get("account_priority_score") or 0),
            c["company"]["company_id"],
        ),
    )


def _order_review(cards: list[dict]) -> list[dict]:
    return sorted(
        cards,
        key=lambda c: (
            len(c["gate_result"]["blockers"]),
            0 if c["book_lanes"] else 1,
            -(c.get("account_priority_score") or 0),
            -parse_date(c["observed"]["activity_date"]).toordinal(),
            c["company"]["company_id"],
        ),
    )


def assemble(conn: sqlite3.Connection, as_of: datetime, *, outcomes: dict | None = None) -> dict:
    health = collect_health(conn, as_of)
    since = (as_of - timedelta(days=G.REVIEW_WINDOW_DAYS)).date().isoformat()
    until = as_of.date().isoformat()
    permits = _classified(recent_permits(conn, since=since, until=until), as_of)
    wet_by_company: dict[int, list[dict]] = {}
    for p in permits:
        if p["scope"]["wet_scopes"]:
            wet_by_company.setdefault(int(p["company_id"]), []).append(p)
    ids = sorted(wet_by_company)
    ctx = company_context(conn, ids)
    attach_name_peers(ctx, name_peer_index(conn))
    outcomes = outcomes if outcomes is not None else {}

    cards, unassigned = [], 0
    for cid in ids:
        item = ctx.get(cid) or {}
        if not item.get("lanes"):
            unassigned += 1
            continue
        card = build_card(item, wet_by_company[cid], health, outcomes.get(cid))
        if card is not None:
            cards.append(card)

    queue = _order_queue([c for c in cards if c["action"] in C.QUEUE_ACTIONS])[: C.QUEUE_MAX]
    dnc = [c for c in cards if c["action"] == C.DO_NOT_CONTACT]
    review_pool = [c for c in cards if c["action"] not in C.QUEUE_ACTIONS and c["action"] != C.DO_NOT_CONTACT]
    review = _order_review(review_pool)[: C.REVIEW_MAX]
    for i, c in enumerate(queue, start=1):
        c["rank"] = i

    refresh_only = [
        c for c in cards if {b["gate"] for b in c["gate_result"]["blockers"]} == {"refresh_recent"}
    ]
    unattributed = _classified(recent_permits(conn, since=since, until=until, attributed=False), as_of)
    unattributed_wet = [p for p in unattributed if p["scope"]["wet_scopes"]]
    quiet_top = _quiet_top_accounts(conn, set(wet_by_company))
    return {
        "health": health,
        "queue": queue,
        "review": review,
        "do_not_contact": _order_review(dnc)[: C.DNC_MAX],
        "counts": {
            "attributed_permits_in_window": len(permits),
            "wet_permits_in_window": sum(len(v) for v in wet_by_company.values()),
            "companies_with_wet_activity": len(ids),
            "companies_without_lane_assignment": unassigned,
            "cards_evaluated": len(cards),
            "by_action": dict(Counter(c["action"] for c in cards)),
            "unattributed_wet_permits_in_window": len(unattributed_wet),
            "stored_why_now_disagreements": sum(1 for c in cards if c["stored_why_now"]["disagreements"]),
            "blocked_only_by_refresh": len(refresh_only),
            "blocked_only_by_refresh_examples": [c["company"]["display_name"] for c in _order_review(refresh_only)[:3]],
            "blocker_frequency": dict(
                Counter(b["gate"] for c in cards for b in c["gate_result"]["blockers"]).most_common()
            ),
        },
        "quiet_top_accounts": quiet_top,
        "window": {"since": since, "until": until, "call_window_days": G.CALL_WINDOW_DAYS},
    }


def _quiet_top_accounts(conn: sqlite3.Connection, active_ids: set[int], top: int = 25) -> dict:
    from pipeline.trust.evidence import PROFILE_KEY

    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT a.company_id, a.account_priority_score, a.trade_identity, c.display_name
                FROM company_customer_priority a JOIN companies c ON c.id = a.company_id
                WHERE a.profile_key = ?
                ORDER BY a.account_priority_score DESC, a.company_id
                LIMIT ?
                """,
                (PROFILE_KEY, top),
            )
        ]
    except sqlite3.Error:
        return {"status": "UNKNOWN"}
    quiet = [r for r in rows if int(r["company_id"]) not in active_ids]
    return {"status": "KNOWN", "top_n": len(rows), "quiet_count": len(quiet), "examples": quiet[:5]}


