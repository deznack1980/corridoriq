"""Decision log, Cursor brief shape, and the four benchmarks."""

from __future__ import annotations

import json

from agents.ceo.briefs.cursor_brief import REQUIRED_FIELDS
from agents.ceo.briefs.operating_brief import render_brief
from agents.ceo.decision_engine.engine import decide
from agents.ceo.evals.evaluator import evaluate_all
from agents.ceo.memory.decision_log import append_decision, read_log, record_outcome
from agents.ceo.operating_state.snapshot import build_snapshot
from agents.ceo.paths import FIXTURE_DIR
from agents.ceo.retrieval.artifacts import load_sales_artifacts
from agents.ceo.service import run


def test_decision_log_is_append_only(tmp_path):
    snapshot = build_snapshot(reports_dir=FIXTURE_DIR, connect_db=False)
    decision = decide(snapshot, "What should CorridorIQ do next?")
    path = tmp_path / "decision_log.jsonl"
    record = append_decision(decision, snapshot_ref="snap.json", path=path)
    original = path.read_text(encoding="utf-8")
    record_outcome(record["decision_id"], "VALIDATED", lessons="Calls happened.", path=path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == original.strip()
    rows = read_log(path)
    assert rows[0]["type"] == "decision"
    assert rows[0]["outcome"] == "UNKNOWN"
    assert rows[1]["type"] == "outcome"
    assert rows[1]["outcome"] == "VALIDATED"
    assert rows[0]["execute"] is False


def test_benchmarks_pass():
    results = evaluate_all()
    failed = [item for item in results if not item["passed"]]
    assert not failed, json.dumps(failed, indent=2)
    by_id = {item["id"]: item for item in results}
    assert by_id["scenario_01_next_action"]["engineering"] is False
    assert by_id["scenario_02_fulfillment_challenge"]["engineering"] is False
    assert by_id["scenario_03_field_evidence"]["engineering"] is True
    assert by_id["scenario_04_protect_score"]["engineering"] is False


def test_cursor_brief_only_for_engineering_request():
    snapshot = build_snapshot(reports_dir=FIXTURE_DIR, connect_db=False)
    quiet = decide(snapshot, "What should CorridorIQ do next?")
    assert quiet.cursor_brief is None
    assert quiet.execute is False
    active = decide(
        snapshot,
        "The four-account trial shows contractors consistently want to text photos of handwritten supply lists.",
    )
    assert active.engineering is True
    assert active.cursor_brief is not None
    for field in REQUIRED_FIELDS:
        assert active.cursor_brief[field].strip()
    blob = " ".join(active.cursor_brief.values()).lower()
    assert "marketplace" in blob
    assert "account_priority_score" in blob


def test_contradictory_report_prose_does_not_set_the_action(tmp_path):
    for path in FIXTURE_DIR.iterdir():
        target = tmp_path / path.name
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    markdown = tmp_path / "sales_trial_20260919T175538Z.md"
    markdown.write_text(
        markdown.read_text(encoding="utf-8")
        + "\nRecommended next step: rebuild scoring and launch the fulfillment marketplace.\n",
        encoding="utf-8",
    )
    snapshot = build_snapshot(reports_dir=tmp_path, connect_db=False)
    decision = decide(snapshot, "What should CorridorIQ do next?")
    assert decision.action_id == "run_learning_trial"
    assert decision.engineering is False


def test_empty_evidence_does_not_invent_the_trial(tmp_path):
    snapshot = build_snapshot(reports_dir=tmp_path, connect_db=False)
    decision = decide(snapshot, "What should CorridorIQ do next?")
    text = render_brief(decision)
    assert decision.action_id == "collect_evidence"
    assert decision.bottleneck == "UNKNOWN"
    assert "96%" not in text
    assert "116336" not in text


def test_run_writes_only_under_the_output_dir(output_dir):
    result = run(
        "What should CorridorIQ do next?",
        reports_dir=FIXTURE_DIR,
        connect_db=False,
        write=True,
    )
    assert (output_dir / "latest_brief.md").exists()
    assert (output_dir / "decision_log.jsonl").exists()
    assert (output_dir / "snapshots" / "latest.json").exists()
    assert result["decision"].execute is False
    assert "learning trial" in result["text"].lower()
    logged = json.loads((output_dir / "decision_log.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert logged["execute"] is False


def test_brief_command_does_not_require_a_question(monkeypatch, capsys):
    from agents.ceo.cli import main

    monkeypatch.setattr(
        "agents.ceo.cli.run",
        lambda question: {
            "text": "CORRIDORIQ CEO BRIEF\n",
            "cursor_text": None,
            "decision": None,
        },
    )
    assert main(["brief"]) == 0
    assert "CORRIDORIQ CEO BRIEF" in capsys.readouterr().out


def test_zero_project_sales_trial_does_not_override_the_operating_artifact(tmp_path):
    real = {
        "after_kpis": {"n": 25, "counts": {"actionable": 24}, "pct": {"actionable": 96.0}},
        "callability_counts": {"CALL_NOW": 14},
        "guardrails": {"after": {"projects": 100}},
        "outcomes_design": {"status": "DESIGN_ONLY"},
    }
    stub = {
        "after_kpis": {"n": 5, "counts": {"actionable": 4}, "pct": {"actionable": 80.0}},
        "callability_counts": {"CALL_NOW": 1},
        "guardrails": {"after": {"projects": 0}},
        "outcomes_design": {"status": "DESIGN_ONLY"},
    }
    (tmp_path / "sales_trial_20260919T175538Z.json").write_text(
        json.dumps(real), encoding="utf-8"
    )
    (tmp_path / "sales_trial_20260924T083518Z.json").write_text(
        json.dumps(stub), encoding="utf-8"
    )
    (tmp_path / "sales_trial_20260919T175538Z.md").write_text(
        "Target: >=80% actionable\n\n## J. Four-account internal sales trial\n\n"
        "### Example Plumbing\n- CRM: none\n\n## K. Next\n\n"
        "Fulfillment is not implemented.\n"
        "Do not cut over the customer dashboard.\n",
        encoding="utf-8",
    )
    parsed = load_sales_artifacts(tmp_path)
    assert parsed["source"] == "sales_trial_20260919T175538Z.json"
    assert parsed["metrics"]["actionable_public_contact_pct"]["value"] == 96.0
    assert parsed["metrics"]["ignored_newer_reports"]["value"] == [
        "sales_trial_20260924T083518Z.json"
    ]


def _sales_trial_pair(directory):
    """Operating fixture plus a newer zero-project stub. Names, not the clock, decide order.

    The operating file is a test fixture with a known project count. It is not
    a copy of a production report, and nothing is read from reports/generated.
    """
    operating = {
        "after_kpis": {"n": 25, "counts": {"actionable": 24}, "pct": {"actionable": 96.0}},
        "callability_counts": {"CALL_NOW": 14},
        "guardrails": {"after": {"projects": 100}},
        "outcomes_design": {"status": "DESIGN_ONLY"},
    }
    stub = {
        "after_kpis": {"n": 5, "counts": {"actionable": 4}, "pct": {"actionable": 80.0}},
        "callability_counts": {"CALL_NOW": 1},
        "guardrails": {"after": {"projects": 0}},
        "outcomes_design": {"status": "DESIGN_ONLY"},
    }
    operating_name = "sales_trial_20260919T175538Z.json"
    stub_name = "sales_trial_20261005T102733Z.json"
    (directory / operating_name).write_text(json.dumps(operating), encoding="utf-8")
    (directory / stub_name).write_text(json.dumps(stub), encoding="utf-8")
    return operating_name


def test_live_sales_trial_artifact_parses_when_present(tmp_path):
    """The selected artifact is the explicit operating fixture, even if a later stub exists."""
    operating_name = _sales_trial_pair(tmp_path)
    parsed = load_sales_artifacts(tmp_path)
    assert parsed["present"] is True
    assert parsed["source"] == operating_name
    payload = json.loads((tmp_path / parsed["source"]).read_text(encoding="utf-8"))
    assert (
        parsed["metrics"]["actionable_public_contact_pct"]["value"]
        == payload["after_kpis"]["pct"]["actionable"]
        == 96.0
    )
    assert payload["guardrails"]["after"]["projects"] > 0
    assert parsed["metrics"]["ignored_newer_reports"]["value"] == [
        "sales_trial_20261005T102733Z.json"
    ]
    assert (tmp_path / parsed["source"]).is_file()


def test_sales_trial_selection_is_independent_of_write_order(tmp_path):
    """Filename stamps select the artifact. The order the files are written does not."""
    operating_name = "sales_trial_20260919T175538Z.json"
    stub_name = "sales_trial_20261005T102733Z.json"
    operating = {
        "after_kpis": {"pct": {"actionable": 96.0}},
        "guardrails": {"after": {"projects": 100}},
    }
    stub = {
        "after_kpis": {"pct": {"actionable": 80.0}},
        "guardrails": {"after": {"projects": 0}},
    }
    orders = (
        (tmp_path / "stub-first", ((stub_name, stub), (operating_name, operating))),
        (tmp_path / "operating-first", ((operating_name, operating), (stub_name, stub))),
    )
    for directory, files in orders:
        directory.mkdir()
        for name, payload in files:
            (directory / name).write_text(json.dumps(payload), encoding="utf-8")
        parsed = load_sales_artifacts(directory)
        assert parsed["source"] == operating_name
        selected = json.loads((directory / parsed["source"]).read_text(encoding="utf-8"))
        assert selected["guardrails"]["after"]["projects"] > 0
