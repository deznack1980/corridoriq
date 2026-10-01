"""CEO agent tests. They do not open the production database."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_output(tmp_path, monkeypatch):
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path / "ceo-out"))
    monkeypatch.setenv("CEO_ENABLE_MODEL_NARRATION", "0")
    monkeypatch.delenv("CEO_MODEL_API_KEY", raising=False)


@pytest.fixture
def output_dir(tmp_path) -> Path:
    return tmp_path / "ceo-out"
