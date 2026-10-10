"""Mandatory cross-version check: production-schema database + integrated code.

Runs scripts/ops/cross_version_check.py, which exports the production commit
with ``git archive`` and drives three phases against one synthetic database:
production creates it, this code serves it, production serves it again
(rollback). Skipped when the production commit is not in the local history
(for example a shallow CI clone).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PRODUCTION_REF = "fcf50fe"


def _have_ref() -> bool:
    try:
        return subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"{PRODUCTION_REF}^{{commit}}"],
                              capture_output=True).returncode == 0
    except OSError:
        return False


@pytest.mark.skipif(not _have_ref(), reason=f"production commit {PRODUCTION_REF} not available")
def test_production_database_with_integrated_code_and_rollback():
    proc = subprocess.run([sys.executable, str(REPO / "scripts" / "ops" / "cross_version_check.py"),
                           "--production-ref", PRODUCTION_REF], capture_output=True, text=True, timeout=1500)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "FAIL" not in proc.stdout
