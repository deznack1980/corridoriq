"""Retrieval provenance, secrets, authority, and cost limits."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agents.ceo.advisors import advise
from agents.ceo.operating_state.snapshot import build_snapshot
from agents.ceo.paths import FIXTURE_DIR, PACKAGE_DIR, REPO_ROOT
from agents.ceo.retrieval.retriever import clear_cache, retrieve
from agents.ceo.security import customer_safe_output, redact
from agents.framework.limits import CallLimitExceeded, CallLimiter, clip
from agents.framework.provider import ModelDisabled, ModelProvider, narration_enabled


def test_retrieval_keeps_provenance(tmp_path):
    clear_cache()
    doc = tmp_path / "note.md"
    doc.write_text(
        "# Note\n\n## Signal\n\nThe zebra-conduit token marks this chunk.\n",
        encoding="utf-8",
    )
    rows = retrieve("zebra-conduit token", extra_paths=[doc], limit=3)
    assert rows
    row = rows[0]
    assert row["source"]
    assert row["category"]
    assert row["confidence"]
    assert "timestamp" in row
    assert "zebra-conduit" in row["text"]


def test_report_chunks_scrub_phone_numbers(tmp_path):
    clear_cache()
    report = tmp_path / "sales_trial_20260919T175538Z.md"
    report.write_text(
        "## Reach\n\nCall (602) 344-9027 or office@example.com about zebra-conduit.\n",
        encoding="utf-8",
    )
    rows = retrieve("zebra-conduit", extra_paths=[report], limit=2)
    assert rows
    assert "(602) 344-9027" not in rows[0]["text"]
    assert "office@example.com" not in rows[0]["text"]


def test_redact_secrets_and_snapshot_ignores_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CEO_MODEL_API_KEY", "sk-testsecretSHOULDNOTAPPEAR")
    monkeypatch.setenv("CORRIDORIQ_R2_SECRET_ACCESS_KEY", "r2secretSHOULDNOTAPPEAR")
    snapshot = build_snapshot(reports_dir=FIXTURE_DIR, connect_db=False)
    blob = json.dumps(snapshot)
    assert "sk-testsecretSHOULDNOTAPPEAR" not in blob
    assert "r2secretSHOULDNOTAPPEAR" not in blob
    assert "sk-testsecretSHOULDNOTAPPEAR" not in redact(
        "key sk-testsecretSHOULDNOTAPPEAR"
    )
    assert "[REDACTED]" in redact("api_key=supersecretvalue")


def test_customer_output_refused():
    with pytest.raises(PermissionError):
        customer_safe_output("hello")


def test_advisors_are_not_built():
    with pytest.raises(NotImplementedError):
        advise("sales", {})


def test_call_limiter_and_context_clip():
    limiter = CallLimiter(2)
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(CallLimitExceeded):
        limiter.acquire()
    clipped = clip("x" * 50, limit=10)
    assert clipped.startswith("x" * 10)
    assert "truncated" in clipped


def test_narration_disabled_by_default():
    assert narration_enabled() is False
    with pytest.raises(ModelDisabled):
        ModelProvider().complete([{"role": "user", "content": "hi"}], purpose="test")


def test_framework_does_not_import_ceo_knowledge():
    framework = REPO_ROOT / "agents" / "framework"
    for path in framework.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import agents.ceo" not in text
        assert "from agents.ceo" not in text
        assert "PLUMBING_CORE" not in text


def test_production_modules_do_not_take_write_paths():
    banned = (
        "import subprocess",
        "from subprocess",
        "os.system(",
        "subprocess.",
        "get_connection(",
        "DROP TABLE",
        "DELETE FROM",
        "INSERT INTO",
    )
    roots = [
        PACKAGE_DIR / "operating_state",
        PACKAGE_DIR / "decision_engine",
        PACKAGE_DIR / "retrieval",
        PACKAGE_DIR / "briefs",
        PACKAGE_DIR / "memory",
        PACKAGE_DIR / "service.py",
        PACKAGE_DIR / "cli.py",
    ]
    for root in roots:
        paths = [root] if root.is_file() else list(root.glob("*.py"))
        for path in paths:
            text = path.read_text(encoding="utf-8")
            for token in banned:
                assert token not in text, f"{path.name} contains {token}"


def test_engine_does_not_hardcode_cohort_results():
    text = ""
    for path in (PACKAGE_DIR / "decision_engine").glob("*.py"):
        text += path.read_text(encoding="utf-8")
    assert "PARKER" not in text
    assert "96.0" not in text
    assert "96%" not in text
