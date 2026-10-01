"""Morning Operator run. Opens the database read-only and writes only under
reports/generated/ceo/morning/.

The candidate and gate logic lives in pipeline.trust so the sales portal uses
the same rules. This module adds the CEO-only parts: truth cards, the run-to-run
diff, the CEO summary, and the artifacts.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from pipeline.trust import contract as C
from pipeline.trust import gates as G
from pipeline.trust import scope as S
from pipeline.trust.dates import parse_ts, phoenix_now, utc_now
from pipeline.trust.evidence import attach_name_peers, company_context, name_peer_index, recent_permits
from pipeline.trust.opportunities import _classified, _clean, assemble, build_card

from agents.ceo.morning.outcomes import latest_by_company
from agents.ceo.operating_state.collector import connect_readonly, resolve_db_path

TRUTH_CARD_IDS = {
    37857: "Parker & Sons",
    38349: "Kerns Plumbing",
    47437: "Kerns Plumbing L L C",
    40543: "Umbrella Plumbing",
    40584: "Advanced Plumbing and Piping",
}

def morning_dir() -> Path:
    from agents.ceo.paths import output_dir

    return output_dir() / "morning"


def _db_fingerprint(path: Path) -> dict:
    """Size and mtime of the files that hold data. The -shm index is touched by
    every reader, read-only ones included, and a reader may create an empty
    -wal file, so neither is evidence of a write."""
    out = {}
    for suffix in ("", "-wal"):
        p = Path(str(path) + suffix)
        if p.exists() and (suffix == "" or p.stat().st_size > 0):
            st = p.stat()
            out[p.name] = {"size": st.st_size, "mtime_ns": st.st_mtime_ns}
    return out


def truth_cards(conn: sqlite3.Connection, as_of: datetime, health: dict, outcomes: dict | None = None) -> list[dict]:
    """The four known cases, evaluated with the same rules and a one-year lookback."""
    ids = sorted(TRUTH_CARD_IDS)
    since = (as_of - timedelta(days=365)).date().isoformat()
    permits = _classified(
        recent_permits(conn, since=since, until=as_of.date().isoformat(), company_ids=ids), as_of
    )
    ctx = company_context(conn, ids)
    attach_name_peers(ctx, name_peer_index(conn))
    out = []
    for cid in ids:
        mine = sorted(
            [p for p in permits if int(p["company_id"]) == cid],
            key=lambda p: (p["activity_date"], p["permit_id"]),
            reverse=True,
        )
        wet = [p for p in mine if p["scope"]["wet_scopes"]]
        in_window = [p for p in wet if p["activity_days"] <= G.REVIEW_WINDOW_DAYS]
        item = ctx.get(cid) or {}
        card = build_card(item, in_window, health, (outcomes or {}).get(cid)) if in_window else None
        latest_by_lane = {}
        for p in wet:
            for lane in p["scope"]["lanes"]:
                latest_by_lane.setdefault(lane, {
                    "permit_number": p.get("permit_number"),
                    "jurisdiction": p.get("jurisdiction"),
                    "activity_date": p["activity_date"],
                    "scope": S.LABELS[S.scope_for_lane(p["scope"], lane) or p["scope"]["primary"]],
                    "description": _clean(p.get("description") or p.get("project_description"), 120),
                })
        pri = item.get("priority") or {}
        out.append(
            {
                "company_id": cid,
                "label": TRUTH_CARD_IDS[cid],
                "found": item.get("company") is not None,
                "display_name": (item.get("company") or {}).get("display_name"),
                "account_priority_score": pri.get("account_priority_score"),
                "trade_identity": pri.get("trade_identity"),
                "stored_why_now": pri.get("why_now"),
                "stored_latest_date": pri.get("most_recent_relevant_date"),
                "stored_generated_at": pri.get("generated_at"),
                "book_lanes": G.book_lanes(item),
                "identity": G.identity_state(item),
                "contact": G.contact_state(item),
                "latest_permits": [
                    {
                        "permit_number": p.get("permit_number"),
                        "jurisdiction": p.get("jurisdiction"),
                        "activity_date": p["activity_date"],
                        "scope": p["scope"]["primary_label"],
                        "wet": bool(p["scope"]["wet_scopes"]),
                        "description": _clean(p.get("description") or p.get("project_description"), 120),
                    }
                    for p in mine[:6]
                ],
                "latest_wet_by_lane": latest_by_lane,
                "morning_evaluation": None if card is None else {
                    "action": card["action"],
                    "confidence": card["confidence"],
                    "lane": card["derived"]["lane"],
                    "why_now": card["why_now"],
                    "blockers": card["gate_result"]["blockers"],
                    "flags": card["gate_result"]["flags"],
                    "stored_disagreements": card["stored_why_now"]["disagreements"],
                },
            }
        )
    return out


def _diff(current: dict, previous: dict | None) -> dict:
    if not previous:
        return {"status": "FIRST_RUN", "note": "No earlier morning run to compare."}

    def by_company(items):
        return {c["company"]["company_id"]: c for c in items}

    now_q, prev_q = by_company(current["queue"]), by_company(previous.get("queue") or [])
    all_now = by_company(current["queue"] + current["review"] + current["do_not_contact"])
    all_prev = by_company((previous.get("queue") or []) + (previous.get("review") or []) + (previous.get("do_not_contact") or []))
    changed = []
    for cid, card in all_now.items():
        old = all_prev.get(cid)
        if old and old["observed"]["permit_id"] != card["observed"]["permit_id"]:
            changed.append(
                {"company": card["company"]["display_name"], "from_permit": old["observed"]["permit_number"],
                 "to_permit": card["observed"]["permit_number"]}
            )
    prev_stale = set((previous.get("health") or {}).get("stale_sources") or [])
    resolved = []
    for cid, card in all_now.items():
        old = all_prev.get(cid)
        if old and old["company"]["identity"]["status"] not in {G.VERIFIED, G.HIGH_CONFIDENCE} and card["company"][
            "identity"
        ]["status"] in {G.VERIFIED, G.HIGH_CONFIDENCE}:
            resolved.append(card["company"]["display_name"])
    return {
        "status": "COMPARED",
        "previous_generated_at": previous.get("generated_at"),
        "newly_qualifying": [now_q[c]["company"]["display_name"] for c in now_q if c not in prev_q],
        "dropped": [
            {"company": prev_q[c]["company"]["display_name"], "now": (all_now.get(c) or {}).get("action", "NOT_LISTED")}
            for c in prev_q
            if c not in now_q
        ],
        "evidence_changed": changed,
        "newly_stale_sources": sorted(set(current["health"]["stale_sources"]) - prev_stale),
        "newly_resolved_identity": resolved,
    }


def _summary(result: dict) -> dict:
    q, review, health, counts = result["queue"], result["review"], result["health"], result["counts"]
    if q:
        top = q[0]
        leverage = (
            f"{top['action']} {top['company']['display_name']}: {top['derived']['scope_label']} "
            f"permit {top['observed']['permit_number']} ({top['observed']['activity_date']})."
        )
    elif counts["blocker_frequency"]:
        gate, n = next(iter(counts["blocker_frequency"].items()))
        leverage = f"No record passed every gate. Most common blocker: {gate} ({n} accounts). Clearing it is today's highest-leverage work."
    else:
        leverage = "No record passed every gate and no wet-side activity was found in the window."
    risks = []
    if health["refresh"]["state"] in {"STALE", "UNKNOWN"}:
        risks.append(f"Refresh state {health['refresh']['state']}; recency cannot be trusted.")
    if health["stale_sources"]:
        risks.append(
            "Stale or empty sources (successful runs, old records): " + ", ".join(health["stale_sources"]) + "."
        )
    if counts["unattributed_wet_permits_in_window"]:
        risks.append(
            f"{counts['unattributed_wet_permits_in_window']} wet-side permits in the window have no contractor company attribution."
        )
    if counts["stored_why_now_disagreements"]:
        risks.append(
            f"Stored why-now disagrees with observed permits for {counts['stored_why_now_disagreements']} evaluated accounts."
        )
    if counts.get("blocked_only_by_refresh"):
        names = ", ".join(counts["blocked_only_by_refresh_examples"])
        leverage += (
            f" {counts['blocked_only_by_refresh']} accounts fail only the refresh gate (e.g. {names}); "
            "they are not actionable until a current refresh confirms them."
        )
    quiet = result.get("quiet_top_accounts") or {}
    avoid = "Do not work accounts because of account priority alone."
    if quiet.get("status") == "KNOWN" and quiet.get("quiet_count"):
        names = ", ".join(r["display_name"] for r in quiet["examples"][:3])
        avoid = (
            f"{quiet['quiet_count']} of the top {quiet['top_n']} priority accounts have no wet-side permit in "
            f"{G.REVIEW_WINDOW_DAYS} days (e.g. {names}). Historical volume is not current intent."
        )
    return {"highest_leverage": leverage, "major_data_risks": risks or ["None flagged."], "not_today": avoid}


def run_morning(
    *,
    db_path: Path | None = None,
    as_of: datetime | None = None,
    write: bool = True,
    include_truth_cards: bool = True,
) -> dict:
    from agents.ceo.morning.render import render_brief, render_truth_cards
    from agents.ceo.security import redact

    as_of = as_of or phoenix_now()
    path = resolve_db_path(db_path)
    generated = utc_now().isoformat().replace("+00:00", "Z")
    if path is None or not path.exists():
        payload = {
            "generated_at": generated,
            "as_of": as_of.isoformat(),
            "status": "UNKNOWN",
            "note": "database not available; no queue produced",
            "queue": [], "review": [], "do_not_contact": [],
        }
        text = "CORRIDORIQ MORNING OPERATOR\n\nDatabase not available. No queue produced. Nothing was inferred.\n"
        return {"payload": payload, "text": text, "paths": {}}

    before = _db_fingerprint(path)
    conn = connect_readonly(path)
    try:
        query_only = int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
        outcomes = latest_by_company()
        result = assemble(conn, as_of, outcomes=outcomes)
        cards = truth_cards(conn, as_of, result["health"], outcomes) if include_truth_cards else []
    finally:
        conn.close()
    after = _db_fingerprint(path)

    root = morning_dir()
    previous = None
    latest_json = root / "latest_morning_queue.json"
    if latest_json.exists():
        try:
            previous = json.loads(latest_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = None

    payload = {
        "generated_at": generated,
        "as_of": as_of.isoformat(),
        "contract_version": C.CONTRACT_VERSION,
        "status": "KNOWN",
        "output_class": "INTERNAL_ONLY",
        "database": {
            "path_name": path.name,
            "mode": "ro",
            "query_only": query_only,
            "fingerprint_before": before,
            "fingerprint_after": after,
            "unchanged": before == after,
        },
        **result,
        "truth_cards": cards,
        "outcome_vocabulary": list(C.OUTCOMES),
    }
    payload["changes"] = _diff(payload, previous)
    payload["ceo_summary"] = _summary(payload)
    text = redact(render_brief(payload))
    paths = {}
    if write:
        paths = _write(root, payload, text, redact(render_truth_cards(cards, as_of)) if cards else None)
    return {"payload": payload, "text": text, "paths": paths}


def _write(root: Path, payload: dict, text: str, truth_text: str | None) -> dict:
    history = root / "history"
    history.mkdir(parents=True, exist_ok=True)
    stamp = payload["generated_at"].replace("-", "").replace(":", "")
    blob = json.dumps(payload, indent=2, default=str)
    paths = {
        "history_json": history / f"{stamp}_morning_queue.json",
        "history_md": history / f"{stamp}_morning_brief.md",
        "latest_json": root / "latest_morning_queue.json",
        "latest_md": root / "latest_morning_brief.md",
    }
    paths["history_json"].write_text(blob, encoding="utf-8")
    paths["history_md"].write_text(text, encoding="utf-8")
    paths["latest_json"].write_text(blob, encoding="utf-8")
    paths["latest_md"].write_text(text, encoding="utf-8")
    if truth_text:
        paths["truth_cards_md"] = root / "latest_truth_cards.md"
        paths["truth_cards_md"].write_text(truth_text, encoding="utf-8")
    return {k: str(v) for k, v in paths.items()}


def env_as_of() -> datetime | None:
    raw = os.environ.get("CEO_MORNING_AS_OF")
    return parse_ts(raw) if raw else None
