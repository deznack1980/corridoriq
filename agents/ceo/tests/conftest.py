"""CEO agent tests. They do not open the production database."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_output(tmp_path, monkeypatch):
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo-out"))
    monkeypatch.setenv("CEO_ENABLE_MODEL_NARRATION", "0")
    monkeypatch.delenv("CEO_MODEL_API_KEY", raising=False)


@pytest.fixture(autouse=True)
def _healthy_launch_runtime(tmp_path_factory, monkeypatch):
    """Existing health fixtures describe a configured production process."""
    root = tmp_path_factory.mktemp("pilot-health")
    monkeypatch.setenv("CORRIDORIQ_ENV", "production")
    monkeypatch.setenv("CORRIDORIQ_PILOT_ROOT", str(root))


@pytest.fixture
def output_dir(tmp_path) -> Path:
    return tmp_path / "ceo-out"
