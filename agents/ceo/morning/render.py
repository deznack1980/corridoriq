"""Markdown for the morning brief and the truth cards. Internal only.

Contact values (phone numbers, emails) are not printed. The card names the
channel type and its row id; the operator looks the value up.
"""

from __future__ import annotations

from datetime import datetime


def _src(card: dict) -> str:
    s = card["source"]
    return f"{s['jurisdiction']} ({s['freshness']}, newest record {s['newest_record_date']}, run health {s['run_health']})"


def _contact(card: dict) -> str:
    c = card["contact"]
    bits = [c["state"]]
    if c.get("channel_type"):
        bits.append(f"{c['channel_type']} via {c['channel_source']} (channel id {c['channel_id']})")
    if c.get("named_contact"):
        bits.append(f"named: {c['named_contact']}" + (f", {c['named_title']}" if c.get("named_title") else ""))
    if c.get("note"):
        bits.append(c["note"])
    return "; ".join(bits)


def _card(i: int | None, card: dict) -> list[str]:
    obs, der = card["observed"], card["derived"]
    head = f"{i}. " if i is not None else "- "
    ident = card["company"]["identity"]
    lines = [
        f"{head}{card['company']['display_name']} (company {card['company']['company_id']})",
        f"   Action: {card['action']}  |  Confidence: {card['confidence']}  |  id {card['opportunity_id']}",
        f"   Why now: {card['why_now']}",
        f"   Observed evidence: {obs['permit_type'] or 'type n/a'}; status {obs['permit_status'] or 'n/a'}; "
        f"{obs['activity_date_field']} {obs['activity_date']}",
        f"   Trade relevance ({der['lane']}): {card['why_this_trade']}",
        f"   Role: {der['role']['role']} ({der['role']['basis']})",
        f"   Identity: {ident['status']} ({ident['basis']})"
        + (f"; name peers {ident['name_peer_company_ids']} (not merged)" if ident["name_peer_company_ids"] else ""),
        f"   Contact: {_contact(card)}",
        f"   Project: project {obs['project_id']}, permit {obs['permit_number']}",
        f"   Address: {obs['address'] or 'not stated'}{', ' + obs['city'] if obs.get('city') else ''}",
        f"   Source: {_src(card)}",
        f"   Account priority (context only, not a gate): {card.get('account_priority_score')}",
        f"   Recommended: {card['recommended_action']}",
    ]
    if card["uncertainty"]:
        lines.append("   Uncertainty: " + "; ".join(card["uncertainty"]))
    if card["stored_why_now"]["disagreements"]:
        lines.append("   Stored why-now disagrees: " + "; ".join(card["stored_why_now"]["disagreements"]))
    lines.append("   Human verification required: " + " ".join(card["human_verification_required"]))
    lines.append("   Not observed: material demand, BOM/RFQ/PO, buying intent.")
    return lines


def render_brief(payload: dict) -> str:
    h = payload["health"]
    refresh = h["refresh"]
    counts = payload["counts"]
    out = [
        "CORRIDORIQ MORNING OPERATOR",
        f"Generated {payload['generated_at']} | as of {payload['as_of'][:10]} | INTERNAL ONLY | no outreach performed",
        "Product A (supply-house intelligence). Every action below is for a supply-house rep; CorridorIQ does not contact contractors.",
        "",
        "DATA HEALTH",
        f"- Refresh: {refresh['state']}"
        + (f", {refresh['hours_since_last_success']}h since last successful run" if refresh["hours_since_last_success"] is not None else ""),
        f"- Stale or empty sources: {', '.join(h['stale_sources']) or 'none'}",
        f"- Lagging sources (8-21 days): {', '.join(h['lagging_sources']) or 'none'}",
        f"- Degraded run health: {', '.join(h['degraded_sources']) or 'none'}",
    ]
    for s in h["sources"]:
        for w in s["warnings"]:
            out.append(f"- {s['jurisdiction']}: {w}")
    out.append(
        f"- Window {payload['window']['since']}..{payload['window']['until']}: "
        f"{counts['wet_permits_in_window']} attributed wet-side permits across {counts['companies_with_wet_activity']} companies; "
        f"{counts['unattributed_wet_permits_in_window']} wet-side permits with no contractor company."
    )
    out += ["", f"TODAY'S ACTION QUEUE ({len(payload['queue'])})"]
    if not payload["queue"]:
        out.append("No record passed every trust gate. The queue is empty on purpose; it is not padded.")
    for i, card in enumerate(payload["queue"], start=1):
        out += _card(i, card) + [""]
    out += ["", f"HOLD / REVIEW ({len(payload['review'])} shown of {sum(v for k, v in counts['by_action'].items() if k not in {'CALL_NOW', 'EMAIL', 'FOLLOW_UP', 'DO_NOT_CONTACT'})})"]
    for card in payload["review"]:
        out += _card(None, card) + [""]
    if payload["do_not_contact"]:
        out += ["", f"DO NOT CONTACT ({len(payload['do_not_contact'])})"]
        for card in payload["do_not_contact"]:
            out.append(
                f"- {card['company']['display_name']} (company {card['company']['company_id']}): "
                + "; ".join(b["reason"] for b in card["gate_result"]["blockers"] if b["action"] == "DO_NOT_CONTACT")
            )
    ch = payload["changes"]
    out += ["", "WHAT CHANGED SINCE LAST RUN"]
    if ch["status"] == "FIRST_RUN":
        out.append("- " + ch["note"])
    else:
        out.append(f"- Previous run: {ch['previous_generated_at']}")
        out.append(f"- Newly qualifying: {', '.join(ch['newly_qualifying']) or 'none'}")
        out.append(
            "- Dropped: " + (", ".join(f"{d['company']} (now {d['now']})" for d in ch["dropped"]) or "none")
        )
        out.append(
            "- Evidence changed: "
            + (", ".join(f"{e['company']} {e['from_permit']} -> {e['to_permit']}" for e in ch["evidence_changed"]) or "none")
        )
        out.append(f"- Newly stale sources: {', '.join(ch['newly_stale_sources']) or 'none'}")
        out.append(f"- Newly resolved identity: {', '.join(ch['newly_resolved_identity']) or 'none'}")
    s = payload["ceo_summary"]
    out += [
        "",
        "CEO SUMMARY",
        f"- Highest-leverage human action: {s['highest_leverage']}",
        "- Major data risk: " + " ".join(s["major_data_risks"]),
        f"- Not today: {s['not_today']}",
        "",
        f"Gate blockers across {counts['cards_evaluated']} evaluated accounts: "
        + (", ".join(f"{k}={v}" for k, v in counts["blocker_frequency"].items()) or "none"),
        f"Actions: " + (", ".join(f"{k}={v}" for k, v in sorted(counts["by_action"].items())) or "none"),
        f"Database opened read-only (query_only={payload['database']['query_only']}); "
        f"file fingerprint unchanged: {payload['database']['unchanged']}.",
        "Record outcomes with: python -m agents.ceo outcome <opportunity_id> <OUTCOME> [--action ACTION] [--note TEXT]",
    ]
    return "\n".join(out) + "\n"


def render_truth_cards(cards: list[dict], as_of: datetime) -> str:
    out = [f"CORRIDORIQ TRUTH CARDS (as of {as_of.date().isoformat()}, one-year lookback, INTERNAL)", ""]
    for c in cards:
        out.append(f"## {c['label']} (company {c['company_id']})")
        if not c["found"]:
            out += ["Company row not found.", ""]
            continue
        out.append(f"- Display name: {c['display_name']}; priority {c['account_priority_score']}; trade identity {c['trade_identity']}")
        out.append(f"- Book lanes: {', '.join(c['book_lanes']) or 'none'}")
        out.append(f"- Identity: {c['identity']['status']} ({c['identity']['basis']}); name peers {c['identity']['name_peer_company_ids'] or 'none'}; canonical peers {c['identity']['canonical_peer_company_ids'] or 'none'}")
        out.append(f"- Contact: {c['contact']['state']}" + (f" ({c['contact']['note']})" if c["contact"].get("note") else ""))
        out.append(f"- Stored why-now ({c['stored_generated_at']}): {c['stored_why_now']}")
        out.append("- Latest permits (any scope):")
        for p in c["latest_permits"]:
            out.append(f"  - {p['activity_date']} {p['jurisdiction']} {p['permit_number']} [{p['scope']}]: {p['description']}")
        out.append("- Latest wet-side permit by lane:")
        for lane, p in c["latest_wet_by_lane"].items():
            out.append(f"  - {lane}: {p['activity_date']} {p['permit_number']} [{p['scope']}]")
        ev = c["morning_evaluation"]
        if ev is None:
            out.append("- Morning evaluation: no wet-side permit in the 60-day window; not listed.")
        else:
            out.append(f"- Morning evaluation: {ev['action']} / {ev['confidence']} / lane {ev['lane']}")
            out.append(f"  - Why now (rebuilt): {ev['why_now']}")
            for b in ev["blockers"]:
                out.append(f"  - Blocked by {b['gate']}: {b['reason']}")
            for d in ev["stored_disagreements"]:
                out.append(f"  - Stored why-now: {d}")
        out.append("")
    return "\n".join(out) + "\n"
