"""Locations the CEO agent is allowed to read, and the one tree it may write."""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parents[1]
CHARTER_DIR = PACKAGE_DIR / "charter"
KNOWLEDGE_DIR = PACKAGE_DIR / "knowledge"
EVALS_DIR = PACKAGE_DIR / "evals"
FIXTURE_DIR = EVALS_DIR / "fixtures"


def state_dir() -> Path:
    """Packaged company state. Override with CEO_STATE_DIR for another machine."""
    override = os.environ.get("CEO_STATE_DIR")
    if override:
        return Path(override).expanduser()
    return PACKAGE_DIR / "company"


def reports_generated_dir() -> Path:
    return REPO_ROOT / "reports" / "generated"


def output_dir() -> Path:
    """Directory the agent may write. Override with CEO_OUTPUT_DIR in tests."""
    override = os.environ.get("CEO_OUTPUT_DIR")
    if override:
        return Path(override).expanduser()
    return reports_generated_dir() / "ceo"


def ensure_output_dir() -> Path:
    path = output_dir()
    path.mkdir(parents=True, exist_ok=True)
    (path / "snapshots").mkdir(exist_ok=True)
    (path / "cursor_briefs").mkdir(exist_ok=True)
    return path
