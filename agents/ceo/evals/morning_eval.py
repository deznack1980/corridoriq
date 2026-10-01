"""Morning Operator eval scenarios against the synthetic fixture database."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path

from agents.ceo.evals import morning_fixture as F
from agents.ceo.morning import contract as C


def _run(db: Path, as_of: datetime) -> dict:
    from agents.ceo.morning.operator import run_morning

    return run_morning(db_path=db, as_of=as_of, write=False)["payload"]


def _card(payload: dict, company_id: int) -> dict | None:
    for key in ("queue", "review", "do_not_contact"):
        for card in payload[key]:
            if card["company"]["company_id"] == company_id:
                return card
    return None


def _truth(payload: dict, company_id: int) -> dict | None:
    return next((t for t in payload["truth_cards"] if t["company_id"] == company_id), None)


def _parker(p, _):
    c = _card(p, F.PARKER)
    f = []
    if c is None:
        return ["Parker card missing"]
    if c["action"] in C.QUEUE_ACTIONS:
        f.append(f"Parker should not be an outreach action, got {c['action']}")
    if c["observed"]["permit_number"] != "2603318":
        f.append("Parker why-now should use the propane permit, not a newer electrical permit")
    if c["derived"]["lane"] != "FUEL_GAS_PROPANE":
        f.append(f"Parker lane should be FUEL_GAS_PROPANE, got {c['derived']['lane']}")
    if "not plumbing service" not in c["why_now"]:
        f.append("Parker why-now must say the propane work is not plumbing service")
    return f


def _kerns(p, _):
    a, b = _card(p, F.KERNS), _card(p, F.KERNS_LLC)
    f = []
    if a is None or b is None:
        return ["both Kerns rows must be evaluated separately"]
    if a["opportunity_id"] == b["opportunity_id"]:
        f.append("Kerns rows collapsed into one opportunity")
    if a["derived"]["lane"] != "FUEL_GAS_PROPANE" or "fuel-gas" not in a["why_now"]:
        f.append("Kerns 38349 should present its gas-to-pool-heater permit as fuel gas")
    if b["derived"]["scope"] != "FIRE_LINE" or "not general plumbing" not in b["why_now"]:
        f.append("Kerns 47437 fire line must not read as general plumbing")
    if b["action"] in C.QUEUE_ACTIONS:
        f.append("Kerns 47437 (unresolved identity, peer-only contact) must not be outreach")
    if b["contact"]["state"] != "PEER_INHERITED_ONLY":
        f.append("Kerns 47437 contact should be flagged as peer-inherited")
    if F.KERNS_LLC not in a["company"]["identity"]["name_peer_company_ids"]:
        f.append("Kerns 38349 should flag 47437 as an unmerged name peer")
    return f


def _umbrella(p, _):
    c = _card(p, F.UMBRELLA)
    if c is None:
        return ["Umbrella card missing"]
    f = []
    if c["derived"]["scope"] != "FUEL_GAS":
        f.append(f"Umbrella scope should be FUEL_GAS, got {c['derived']['scope']}")
    if "plumbing service permit" in c["why_now"].lower() or "fuel-gas" not in c["why_now"]:
        f.append("Umbrella why-now mislabels the gas line")
    if not c["stored_why_now"]["disagreements"]:
        f.append("Umbrella stored why-now disagreement not flagged")
    if c["action"] in C.QUEUE_ACTIONS:
        f.append("Umbrella gas work is outside its plumbing-only book; should not be outreach")
    return f


def _advanced(p, db):
    f = []
    t = _truth(p, F.ADVANCED)
    lane = (t or {}).get("latest_wet_by_lane", {}).get("PLUMBING_CORE")
    if not lane or lane["activity_date"] != "2026-07-21":
        f.append("truth card should find the 2026-07-21 hot/cold line permit as plumbing")
    july = _run(db, datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc))
    c = _card(july, F.ADVANCED)
    if c is None or c["observed"]["activity_date"] != "2026-07-21":
        f.append("as of 2026-08-05 the July permit should drive Advanced's why-now")
    elif c["action"] not in C.QUEUE_ACTIONS:
        f.append(f"as of 2026-08-05 Advanced should be actionable, got {c['action']}")
    return f


def _gc(p, _):
    c = _card(p, F.GC_HIGH)
    if c is None:
        return ["GC card missing"]
    f = []
    if c["action"] in C.QUEUE_ACTIONS:
        f.append("high-score GC must not be outreach")
    if c["derived"]["role"]["role"] != "GENERAL_CONTRACTOR":
        f.append("GC role not detected")
    return f


def _stale(p, _):
    c = _card(p, F.STALE_SRC_CO)
    f = []
    if c is None or c["action"] != C.HOLD:
        f.append("failing source should produce HOLD")
    if "chandler_az" not in p["health"]["stale_sources"]:
        f.append("chandler should be reported stale despite healthy runs")
    return f


def _clean(p, _):
    c = _card(p, F.CLEAN)
    if c is None:
        return ["clean specialist missing"]
    f = []
    if c["action"] != C.CALL_NOW or c["confidence"] != C.HIGH:
        f.append(f"clean specialist should be CALL_NOW/HIGH, got {c['action']}/{c['confidence']}")
    if not c["evidence_refs"] or not c["observed"]["permit_id"]:
        f.append("evidence reference missing")
    return f


def _no_trust(_, db_dir):
    db = F.build(Path(db_dir) / "no_trust.db", variant="no_trust")
    p = _run(db, F.AS_OF)
    f = []
    if p["queue"]:
        f.append(f"stale refresh should produce an empty queue, got {len(p['queue'])}")
    if any(c["action"] == C.CALL_NOW for c in p["review"] + p["do_not_contact"]):
        f.append("CALL_NOW leaked outside the queue")
    return f


SCENARIOS = (
    ("morning_parker_propane_electrical", _parker),
    ("morning_kerns_dual_company", _kerns),
    ("morning_umbrella_gas_line", _umbrella),
    ("morning_advanced_july_plumbing", _advanced),
    ("morning_high_score_gc", _gc),
    ("morning_stale_source", _stale),
    ("morning_clean_recent_specialist", _clean),
    ("morning_no_trustworthy_opportunities", _no_trust),
)


def evaluate_morning() -> list[dict]:
    results = []
    with tempfile.TemporaryDirectory() as tmp:
        db = F.build(Path(tmp) / "morning.db")
        payload = _run(db, F.AS_OF)
        for name, check in SCENARIOS:
            arg = tmp if check is _no_trust else db
            try:
                failures = check(payload, arg)
            except Exception as exc:  # report, do not crash the eval run
                failures = [f"{type(exc).__name__}: {exc}"]
            results.append(
                {
                    "id": name,
                    "passed": not failures,
                    "failures": failures,
                    "recommendation": f"queue={len(payload['queue'])}",
                    "action_id": "MORNING",
                    "bottleneck": "n/a",
                    "engineering": False,
                }
            )
    return results
