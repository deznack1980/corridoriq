"""Approved CorridorIQ brand palette (owner-approved values).

Gold #c9a44c · Metallic gold #b8912f · Gold on dark #e2c179 ·
Ink #0e1116 · Charcoal #0a0e1a. See docs/design/brand_palette.md.
"""

import re
from pathlib import Path

from pipeline.config.settings import PROJECT_ROOT

REPO = Path(PROJECT_ROOT)
GOLD, METALLIC, GOLD_ON_DARK, INK, CHARCOAL = "#c9a44c", "#b8912f", "#e2c179", "#0e1116", "#0a0e1a"
UNAUTHORIZED_PRIMARY = "#2160d4"
# The pre-approval amber/graphite palette must not return to served assets.
RETIRED = ("#e8952b", "#f2ad52", "#a35e0f", "#0d0f10", "#121517", "#1a1e21", "#23292d", "#0b1729")
RETIRED_RGBA = re.compile(r"rgba\(\s*232\s*,\s*149\s*,\s*43\s*,")


def _css(name):
    return (REPO / name).read_text(encoding="utf-8").lower()


def _token(css, name):
    m = re.search(rf"--{re.escape(name)}\s*:\s*([^;]+);", css)
    return m.group(1).strip() if m else None


def test_portal_tokens_use_approved_values():
    css = _css("portal.css")
    assert _token(css, "accent") == GOLD
    assert _token(css, "accent-strong") == METALLIC
    assert _token(css, "accent-on-dark") == GOLD_ON_DARK
    assert _token(css, "accent-ink") == INK
    assert _token(css, "navy-950") == CHARCOAL
    assert _token(css, "navy-900") == INK
    assert _token(css, "ink") == "var(--navy-900)"
    # Gold may alias the accent, but neither may become the unauthorized blue.
    assert _token(css, "gold") in (GOLD, "var(--accent)")
    assert UNAUTHORIZED_PRIMARY not in (_token(css, "accent"), _token(css, "gold"))


def test_pilot_and_landing_tokens_use_approved_values():
    pilot = _css("pilot.css")
    assert _token(pilot, "accent") == GOLD
    assert _token(pilot, "accent-strong") == METALLIC
    assert _token(pilot, "navy-950") == CHARCOAL
    landing = _css("landing.css")
    assert _token(landing, "amber") == GOLD
    assert _token(landing, "amber-2") == GOLD_ON_DARK
    assert _token(landing, "ink") == CHARCOAL


def test_retired_palette_absent_from_served_assets():
    served = list(REPO.glob("*.css")) + list(REPO.glob("*.html")) + [REPO / "portal-common.js"] \
        + list((REPO / "assets" / "brand").glob("*.svg"))
    for path in served:
        if path.name in ("home.html",):  # legacy page, never served (see server._LEGACY_UNSERVED)
            continue
        text = path.read_text(encoding="utf-8").lower()
        for value in RETIRED:
            assert value not in text, f"{path.name} still uses {value}"
        assert not RETIRED_RGBA.search(text), f"{path.name} still uses the retired amber rgba"


def test_theme_color_is_approved_charcoal():
    for path in REPO.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        for value in re.findall(r'name="theme-color" content="(#[0-9a-fA-F]+)"', text):
            if path.name == "home.html":
                continue
            assert value.lower() == CHARCOAL, path.name


def test_brand_marks_use_gold():
    for name in ("corridoriq-mark-amber.svg", "corridoriq-mark.svg", "favicon.svg"):
        svg = (REPO / "assets" / "brand" / name).read_text(encoding="utf-8").lower()
        assert GOLD in svg, name


def test_brand_palette_doc_lists_approved_values():
    doc = (REPO / "docs" / "design" / "brand_palette.md").read_text(encoding="utf-8").lower()
    for value in (GOLD, METALLIC, GOLD_ON_DARK, INK, CHARCOAL):
        assert value in doc
