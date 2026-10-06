"""Default the test process to an explicit supported environment.

Production/public startup refuses an unset CORRIDORIQ_ENV. Tests that need
another value set or delete it themselves.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _corridoriq_test_environment(monkeypatch):
    if "CORRIDORIQ_ENV" not in os.environ:
        monkeypatch.setenv("CORRIDORIQ_ENV", "test")
