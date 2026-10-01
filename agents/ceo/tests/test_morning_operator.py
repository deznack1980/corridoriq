"""Morning Operator trust gates. Synthetic fixture only; never the production DB."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from agents.ceo.evals import morning_fixture as F
from agents.ceo.evals.morning_eval import evaluate_morning
from agents.ceo.morning import contract as C
from agents.ceo.morning import gates as G
from agents.ceo.morning import scope as S
from agents.ceo.morning.operator import run_morning
from agents.ceo.morning.outcomes import record_outcome
from agents.ceo.operating_state.collector import connect_readonly
from agents.ceo.security import customer_safe_output

MORNING_DIR = Path(__file__).resolve().parents[1] / "morning"


@pytest.fixture
def db(tmp_path) -> Path:
    return F.build(tmp_path / "morning.db")


@pytest.fixture
def payload(db) -> dict:
    return run_morning(db_path=db, as_of=F.AS_OF, write=False)["payload"]


def _card(payload, company_id):
    for key in ("queue", "review", "do_not_contact"):
        for card in payload[key]:
            if card["company"]["company_id"] == company_id:
                return card
    return None


# 1
def test_newer_relevant_permit_beats_stale_stored_why_now(payload):
    card = _card(payload, F.KERNS)
    assert card["observed"]["activity_date"] == "2026-09-01"
    assert card["stored_why_now"]["most_recent_relevant_date"] == "2026-08-06"
    assert any("older than observed" in d for d in card["stored_why_now"]["disagreements"])


def test_activity_date_is_the_issue_date_not_opportunity_date(payload):
    card = _card(payload, F.KERNS)
    assert card["observed"]["activity_date_field"] == "issued_date"
    assert card["derived"]["pipeline_opportunity_date"] == "2026-08-06"
    assert "2026-09-01" in card["why_now"] and "2026-08-06" not in card["why_now"]


# 2
def test_newer_irrelevant_permit_is_not_why_now(payload):
    card = _card(payload, F.PARKER)
    assert card["observed"]["permit_number"] == "2603318"
    assert "EV charger" not in card["why_now"]
    assert card["action"] not in C.QUEUE_ACTIONS


# 3
@pytest.mark.parametrize(
    "text",
    [
        "Gas line from meter to pool heater.",
        "Replace the existing leaking 2-inch underground gas line like for like.",
        "GAS_48FT OF 2IN PE FROM METER TO HEATER (400K).",
        "49LF 3in PE FRM MTR TO POOL HTR(400K) 57LF TO FIRE PIT (100K)",
        "85FT OF 1IN PE FROM METER TO FIREPLACE (50K)",
        "Like for like repipe existing gas system, no new appliances",
    ],
)
def test_fuel_gas_is_not_plumbing_service(text):
    sc = S.classify_permit(permit_type="Residential Plumbing", description=text)
    assert sc["primary"] == S.FUEL_GAS
    assert S.PLUMBING_WATER not in sc["wet_scopes"]
    assert S.primary_lane(sc) == "FUEL_GAS_PROPANE"


def test_propane_tank_is_propane(payload):
    sc = S.classify_permit(
        permit_type="Building",
        description="INSTALL (2) 119 GAL A/G PROPANE TANKS — LIQ PROPANE GAS SYS A/G MOD INSTALL",
    )
    assert sc["primary"] == S.PROPANE
    assert "not plumbing service" in _card(payload, F.PARKER)["why_now"]


# 4
def test_fire_line_is_not_ordinary_plumbing(payload):
    sc = S.classify_permit(
        permit_type="Building", description="MAJESTIC HILTON - FIRE LINE — UNDERGROUND FIRELINE & HYDRANT INSTALL"
    )
    assert sc["primary"] == S.FIRE_LINE
    assert "PLUMBING_CORE" not in sc["lanes"]
    card = _card(payload, F.KERNS_LLC)
    assert card["derived"]["scope"] == S.FIRE_LINE
    assert "not general plumbing" in card["why_now"]


def test_hot_and_cold_lines_are_plumbing():
    sc = S.classify_permit(permit_type="Residential Alteration", description="Replace interior hot and cold lines LIKE FOR LIKE")
    assert sc["primary"] == S.PLUMBING_WATER
    assert sc["explicit"] is True


def test_electrical_is_not_wet():
    for text in ("Installing EV charger circuit", "MPU FROM 125A TO NEW 200A SES W/150A MAIN BREAKER."):
        assert S.classify_permit(permit_type="Residential Electrical", description=text)["wet_scopes"] == []


# 5
def test_gc_association_is_not_plumbing_contractor(payload):
    card = _card(payload, F.GC_HIGH)
    assert card["derived"]["role"]["role"] == G.GC
    assert card["action"] not in C.QUEUE_ACTIONS
    assert card["account_priority_score"] > max(c["account_priority_score"] for c in payload["queue"])


def _gate(**overrides):
    base = dict(
        ctx={"company": {"lifecycle_state": "active"}, "lanes": [{"lane_key": "PLUMBING_CORE", "fit": "HIGH", "presentable": 1}]},
        lane="PLUMBING_CORE",
        scope={"explicit": True},
        activity_days=5,
        source={"freshness": "FRESH", "call_blocked": False, "jurisdiction": "phoenix_az"},
        refresh_state="CURRENT",
        role={"role": G.TRADE_CONTRACTOR, "basis": "permit", "confidence": 90.0, "level": "permit"},
        identity={"status": G.VERIFIED, "basis": "ROC", "flags": []},
        contact={"state": G.VERIFIED_PHONE},
    )
    base.update(overrides)
    return G.evaluate(**base)


def test_clean_gate_passes():
    assert _gate()["action"] == C.CALL_NOW


# 6
def test_unknown_role_cannot_call():
    result = _gate(role={"role": G.UNKNOWN_ROLE, "basis": "none", "confidence": 0.0, "level": "none"})
    assert result["action"] == C.VERIFY_PROJECT_ROLE


# 7
def test_stale_source_cannot_call(payload):
    for freshness in ("STALE", "NO_DATA"):
        result = _gate(source={"freshness": freshness, "call_blocked": True, "jurisdiction": "chandler_az"})
        assert result["action"] == C.HOLD
    lag = _gate(source={"freshness": "LAGGING", "call_blocked": False, "jurisdiction": "x"})
    assert lag["action"] == C.CALL_NOW and lag["confidence"] == C.MEDIUM
    assert _card(payload, F.STALE_SRC_CO)["action"] == C.HOLD
    assert "chandler_az" in payload["health"]["stale_sources"]


# 8
def test_missing_contact_cannot_call(payload):
    for state in (G.NO_CONTACT, G.CANDIDATE_ONLY, G.WEB_ONLY):
        assert _gate(contact={"state": state})["action"] == C.RESEARCH
    assert _card(payload, F.NO_CONTACT_CO)["action"] == C.RESEARCH


# 9
def test_canonical_peer_contact_is_not_same_company_verification(payload):
    assert _gate(contact={"state": G.PEER_ONLY})["action"] == C.VERIFY_CONTRACTOR
    card = _card(payload, F.KERNS_LLC)
    assert card["contact"]["state"] == G.PEER_ONLY
    assert card["action"] not in C.QUEUE_ACTIONS


# 10
def test_kerns_rows_stay_distinct(payload, db):
    a, b = _card(payload, F.KERNS), _card(payload, F.KERNS_LLC)
    assert a["company"]["company_id"] != b["company"]["company_id"]
    assert a["opportunity_id"] != b["opportunity_id"]
    assert F.KERNS_LLC in a["company"]["identity"]["name_peer_company_ids"]
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM companies WHERE display_name LIKE 'KERNS%'").fetchone()[0] == 2
    conn.close()


# 11
def test_every_actionable_why_now_has_evidence(payload):
    assert payload["queue"]
    for card in payload["queue"]:
        assert f"permits.id={card['observed']['permit_id']}" in card["evidence_refs"]
        assert card["observed"]["permit_number"] in card["why_now"]
        assert card["observed"]["activity_date"] in card["why_now"]
        assert card["why_this_trade"].startswith("Permit text contains")
        assert card["not_observed"] == list(C.NOT_OBSERVED_ITEMS)


# 12
def test_queue_is_not_padded(payload):
    assert 0 < len(payload["queue"]) < C.QUEUE_MAX
    assert {c["company"]["company_id"] for c in payload["queue"]} == {F.KERNS, F.CLEAN}
    assert all(c["action"] in C.QUEUE_ACTIONS for c in payload["queue"])


# 13
def test_zero_trustworthy_means_zero_calls(tmp_path):
    db = F.build(tmp_path / "no_trust.db", variant="no_trust")
    p = run_morning(db_path=db, as_of=F.AS_OF, write=False)["payload"]
    assert p["queue"] == []
    assert p["health"]["refresh"]["state"] == "STALE"
    assert "empty on purpose" in run_morning(db_path=db, as_of=F.AS_OF, write=False)["text"]


# 14
def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_database_is_not_mutated(db, tmp_path):
    before = _digest(db)
    result = run_morning(db_path=db, as_of=F.AS_OF, write=True)
    assert _digest(db) == before
    assert result["payload"]["database"]["unchanged"] is True
    assert result["payload"]["database"]["query_only"] is True
    ro = connect_readonly(db)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("UPDATE companies SET display_name = 'x'")
    ro.close()


def test_morning_code_has_no_write_sql():
    forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|REPLACE INTO|CREATE)\b|\.commit\(")
    for path in MORNING_DIR.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "outcomes.py":
            text = text.split("PROPOSED_SQL")[0]  # design-only SQL string is never executed
        assert not forbidden.search(text), path.name


# 15
def test_customer_output_stays_prohibited(payload, db):
    with pytest.raises(PermissionError):
        customer_safe_output(payload)
    assert payload["output_class"] == "INTERNAL_ONLY"
    text = run_morning(db_path=db, as_of=F.AS_OF, write=False)["text"]
    assert "INTERNAL ONLY" in text
    assert "602-555-0100" not in text and "6025550100" not in text


def test_override_that_restates_status_is_not_flagged():
    same = {"overrides": [{"original_match_status": "VERIFIED_MATCH", "recommended_match_status": "VERIFIED_MATCH", "applied": 0}]}
    assert "IDENTITY_OVERRIDE_NOT_APPLIED" not in G.identity_state(same)["flags"]
    none = {"overrides": [{"original_match_status": None, "recommended_match_status": "NO_MATCH", "applied": 0}]}
    assert "IDENTITY_OVERRIDE_NOT_APPLIED" not in G.identity_state(none)["flags"]
    differs = {"overrides": [{"original_match_status": "POSSIBLE_MATCH", "recommended_match_status": "CONFLICT", "applied": 0}]}
    assert "IDENTITY_OVERRIDE_NOT_APPLIED" in G.identity_state(differs)["flags"]


def test_priority_is_not_a_gate(payload):
    for card in payload["queue"] + payload["review"]:
        assert "account_priority" not in " ".join(card["gate_result"]["gates"])


def test_outcome_capture_and_negative_outcome_holds(db):
    first = run_morning(db_path=db, as_of=F.AS_OF, write=True)
    clean = _card(first["payload"], F.CLEAN)
    with pytest.raises(ValueError):
        record_outcome(clean["opportunity_id"], "MAYBE")
    with pytest.raises(LookupError):
        record_outcome("mo-1-1", "RELEVANT")
    row = record_outcome(clean["opportunity_id"], "bad_data", action="CALL_NOW", note="wrong shop; token=abc123")
    assert row["outcome"] == "BAD_DATA" and "abc123" not in row["note"]
    second = run_morning(db_path=db, as_of=F.AS_OF, write=True)["payload"]
    held = _card(second, F.CLEAN)
    assert held["action"] == C.HOLD
    assert second["changes"]["status"] == "COMPARED"
    assert "DESERT FLOW PLUMBING LLC" in [d["company"] for d in second["changes"]["dropped"]]


def test_artifacts_keep_history(db, output_dir):
    run_morning(db_path=db, as_of=F.AS_OF, write=True)
    run_morning(db_path=db, as_of=datetime(2026, 9, 30, 12, tzinfo=timezone.utc), write=True)
    root = output_dir / "morning"
    assert (root / "latest_morning_queue.json").exists()
    assert (root / "latest_morning_brief.md").exists()
    assert (root / "latest_truth_cards.md").exists()
    assert len(list((root / "history").glob("*_morning_queue.json"))) >= 1


def test_morning_evals_pass():
    results = evaluate_morning()
    failed = [r for r in results if not r["passed"]]
    assert not failed, failed
    assert len(results) == 8
