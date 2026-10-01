"""Regression: a page script must not write into an element its page lacks.

The admin dashboard broke when the map redesign removed #topOpps from the HTML
while admin-dashboard.js still did getElementById("topOpps").innerHTML = ...,
which throws and stops the page before any data loads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
# Direct, unguarded use: getElementById("x").something
_DIRECT = re.compile(r'getElementById\(\s*["\']([\w-]+)["\']\s*\)\s*\.')
_IDS = re.compile(r'\bid\s*=\s*["\']([\w-]+)["\']')
# Created at runtime by shared chrome in portal-common.js (header, shell).
_COMMON = REPO / "portal-common.js"


def _pages():
    out = []
    for js in sorted(REPO.glob("*.js")):
        html = js.with_suffix(".html")
        if html.exists() and "portal-common.js" in html.read_text(encoding="utf-8"):
            out.append((html, js))
    return out


@pytest.mark.parametrize("html,js", _pages(), ids=lambda p: p.name)
def test_direct_element_writes_have_targets(html, js):
    page_ids = set(_IDS.findall(html.read_text(encoding="utf-8")))
    script = js.read_text(encoding="utf-8")
    known = page_ids | set(_IDS.findall(script)) | set(_IDS.findall(_COMMON.read_text(encoding="utf-8")))
    missing = sorted({i for i in _DIRECT.findall(script) if i not in known})
    assert not missing, f"{js.name} writes to #{', #'.join(missing)} but {html.name} has no such element"


def test_admin_dashboard_regression_case():
    script = (REPO / "admin-dashboard.js").read_text(encoding="utf-8")
    html = (REPO / "admin-dashboard.html").read_text(encoding="utf-8")
    assert 'getElementById("topOpps")' not in script
    assert 'id="mapWorkspace"' in html and 'id="todaysAccounts"' in html
