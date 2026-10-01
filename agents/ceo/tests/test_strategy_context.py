"""Product A and Product B stay separate in the CEO's durable context."""

from __future__ import annotations

from agents.ceo.decision_engine.engine import decide
from agents.ceo.operating_state.snapshot import build_snapshot
from agents.ceo.paths import CHARTER_DIR, FIXTURE_DIR, KNOWLEDGE_DIR, PACKAGE_DIR
from agents.ceo.retrieval.retriever import retrieve


def _facts() -> dict[str, str]:
    text = (KNOWLEDGE_DIR / "commercial_models.md").read_text(encoding="utf-8")
    out = {}
    for line in text.splitlines():
        if line.startswith("FACT "):
            key, _, value = line[5:].partition(":")
            out[key.strip()] = value.strip()
    return out


def test_commercial_facts_keep_products_separate():
    facts = _facts()
    assert facts["products_are_separate"] == "true"
    assert facts["blend_products"] == "false"
    assert facts["assume_marketplace"] == "false"
    assert facts["product_b_authorized"] == "false"
    assert "supply houses" in facts["product_a"] and "current commercial focus" in facts["product_a"]
    assert "future hypothesis" in facts["product_b"]


def test_old_parallel_lane_framing_is_gone_from_active_context():
    for path in list(KNOWLEDGE_DIR.glob("*.md")) + list(CHARTER_DIR.glob("*.md")) + list(PACKAGE_DIR.rglob("*.py")):
        if "tests" in path.parts:
            continue
        assert "parallel lanes on one engine" not in path.read_text(encoding="utf-8"), path


def test_snapshot_carries_the_separation():
    snap = build_snapshot(reports_dir=FIXTURE_DIR, connect_db=False)
    commercial = snap["commercial"]
    assert "supply houses" in commercial["product_a"]["value"]
    assert "future hypothesis" in commercial["product_b"]["value"]
    assert commercial["products_separate"]["value"] is True
    assert commercial["assume_marketplace"]["value"] is False


def test_blended_question_is_still_refused():
    snap = build_snapshot(reports_dir=FIXTURE_DIR, connect_db=False)
    question = "Should we turn our supply-house subscribers into a fulfillment marketplace now?"
    decision = decide(snap, question, retrieve(question, reports_dir=FIXTURE_DIR, limit=4))
    assert decision.engineering is False
    assert decision.cursor_brief is None
    assert any("marketplace" in str(item).lower() for item in decision.do_not)
