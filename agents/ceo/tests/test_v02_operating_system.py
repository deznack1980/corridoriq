"""v0.2 company state, gates, brief, and Jarvis contract."""

from __future__ import annotations

import pytest

from agents.ceo.approval import execute, propose, requires_approval
from agents.ceo.briefs.operating_brief import render_brief
from agents.ceo.company.loader import capability, committed_price, kpi_view, load_company_state, may_market_as_live
from agents.ceo.company.signals import FORBIDDEN, sonoran_john
from agents.ceo.decision_engine.engine import decide
from agents.ceo.jarvis import SCHEMA, answer
from agents.ceo.operating_state.snapshot import build_snapshot
from agents.ceo.paths import FIXTURE_DIR, state_dir
from agents.ceo.retrieval.retriever import retrieve


def test_constitution_and_two_products_load():
    state = load_company_state()
    constitution = state["constitution"]
    assert constitution["company"] == "CorridorIQ"
    assert constitution["category"] == "Construction Intelligence + Procurement"
    assert constitution["products_are_connected"] is True
    assert constitution["assume_marketplace"] is False
    assert constitution["commercial_thesis"]["transaction_fee_default"] is False
    assert "supply houses / distributors" in constitution["primary_users"]
    assert "contractors" in constitution["primary_users"]


def test_maturity_does_not_promote_roadmap():
    state = load_company_state()
    assert capability(state, "supplier_intelligence")["status"] == "LIVE"
    assert capability(state, "contractor_material_request_text")["status"] == "EARLY_ACCESS"
    for key in (
        "photo_to_bom",
        "pdf_ingestion",
        "spreadsheet_ingestion",
        "live_inventory",
        "account_specific_pricing",
        "automated_quoting",
        "quote_comparison",
        "payments",
        "checkout",
        "multi_supplier_marketplace",
        "automated_compatibility",
    ):
        assert capability(state, key)["status"] == "PLANNED"
        assert may_market_as_live(state, key) is False


def test_sonoran_signal_is_not_revenue():
    state = load_company_state()
    signal = sonoran_john(state)
    assert signal["signal"] == "POSITIVE_CUSTOMER_DEVELOPMENT_SIGNAL"
    assert signal["willingness_to_pay"] == "UNKNOWN"
    assert signal["conversion_status"] == "not_a_customer"
    assert signal["pilot_status"] == "not_an_active_paid_pilot"
    for label in FORBIDDEN:
        assert label in signal["must_not_classify_as"]
    assert signal["FACT"].startswith("John expressed")
    assert "not a purchase" in signal["INFERENCE"]
    assert signal["HYPOTHESIS"].startswith("A $750/month")
    assert "hypothesis" in signal["HYPOTHESIS"].lower()
    assert "Demonstrate" in signal["RECOMMENDATION"]


def test_kpis_stay_unknown_and_reject_fabricated_values():
    state = load_company_state()
    for group in state["kpis"]["groups"].values():
        for name, metric in group.items():
            assert metric["status"] == "UNKNOWN", name
            assert metric["value"] is None
    with pytest.raises(ValueError):
        kpi_view({"groups": {"supplier_commercial": ["mrr"]}, "values": {"mrr": 750}})


def test_pricing_and_inventory_are_not_committed():
    state = load_company_state()
    assert committed_price(state) is None
    assert state["pricing"]["claim_class"] == "HYPOTHESIS"
    assert state["pricing"]["transaction_fee_default"] is False
    assert "inventory" in state["maturity"]["ai_must_not_fabricate"]
    assert "price" in state["maturity"]["ai_must_not_fabricate"]


def test_launch_is_not_marked_public():
    state = load_company_state()
    assert state["launch"]["publicly_launched"] is False
    assert state["launch"]["landing_commit"] == "b2a532a"
    assert state["launch"]["landing_commit_is_cutover"] is False
    assert "DNS cutover" in state["launch"]["remaining_before_public_launch"]


def test_approval_gates_never_execute():
    assert requires_approval("dns_change") is True
    assert requires_approval("customer_outreach") is True
    assert requires_approval("pricing_commitment") is True
    assert requires_approval("analyze") is False
    proposal = propose("email")
    assert proposal["execute"] is False
    assert proposal["performed"] is False
    assert proposal["requires_founder_approval"] is True
    with pytest.raises(PermissionError):
        execute("email")


def test_cloud_state_dir_is_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("CEO_STATE_DIR", str(tmp_path))
    assert state_dir() == tmp_path
    monkeypatch.delenv("CEO_STATE_DIR")
    assert state_dir().name == "company"


def test_executive_brief_and_jarvis_schema():
    snapshot = build_snapshot(reports_dir=FIXTURE_DIR, connect_db=False)
    question = "What should CorridorIQ do next?"
    decision = decide(snapshot, question, retrieve(question, reports_dir=FIXTURE_DIR, limit=2))
    text = render_brief(decision)
    for heading in (
        "1. COMPANY STATUS",
        "3. REVENUE",
        "5. CUSTOMER SIGNALS",
        "11. TOP 3 PRIORITIES TODAY",
        "13. WHAT NOT TO WORK ON",
        "FACT:",
        "INFERENCE:",
        "HYPOTHESIS:",
        "RECOMMENDATION:",
        "MRR UNKNOWN",
        "Publicly launched: no",
        "Authorized to execute: no",
    ):
        assert heading in text
    assert "live inventory: PLANNED" in text.lower() or "Live inventory: PLANNED" in text
    payload = answer("blocking_revenue", snapshot=snapshot, decision=decision)
    assert payload["schema"] == SCHEMA
    assert payload["execute"] is False
    assert payload["body"]["mrr"] == "UNKNOWN"
    assert payload["body"]["validated_willingness_to_pay"] == "UNKNOWN"
    nxt = answer("next_action", decision=decision)
    assert nxt["body"]["execute"] is False
    assert nxt["body"]["proposal"]["performed"] is False
