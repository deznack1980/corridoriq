"""Guard the approved amber-gold brand tokens against an unauthorized
blue primary. Informational --blue is allowed.
"""

from pathlib import Path

from pipeline.config.settings import PROJECT_ROOT

REPO = Path(PROJECT_ROOT)
UNAUTHORIZED_PRIMARY = "#2160d4"
APPROVED_GOLD = "#c9a44c"
APPROVED_GOLD_STRONG = "#b8912f"
APPROVED_INK = "#0e1116"


def test_portal_tokens_restore_approved_gold():
    css = (REPO / "portal.css").read_text(encoding="utf-8")
    assert f"--gold: {APPROVED_GOLD};" in css
    assert f"--accent: {APPROVED_GOLD};" in css
    assert f"--gold-strong: {APPROVED_GOLD_STRONG};" in css
    assert f"--ink: {APPROVED_INK};" in css
    assert "--gold: var(--accent)" not in css
    assert UNAUTHORIZED_PRIMARY not in css.lower()
    assert "--ci-primary:" in css
    assert "--blue: #3b74c4" in css


def test_public_and_auth_css_do_not_reintroduce_blue_brand():
    for name in ("site.css", "auth-public.css", "pilot.css"):
        text = (REPO / name).read_text(encoding="utf-8").lower()
        assert UNAUTHORIZED_PRIMARY not in text, name
        assert "#1a4eb0" not in text, name
        assert "#8db3f5" not in text, name
        assert "#1d4fbf" not in text, name


def test_home_illustration_uses_approved_gold():
    html = (REPO / "home.html").read_text(encoding="utf-8")
    assert APPROVED_GOLD in html
    assert UNAUTHORIZED_PRIMARY not in html
    assert "#8db3f5" not in html


def test_brand_palette_doc_exists():
    doc = (REPO / "docs" / "design" / "brand_palette.md").read_text(encoding="utf-8")
    assert APPROVED_GOLD in doc
    assert "do not set" in doc.lower() or "not" in doc.lower()
    assert UNAUTHORIZED_PRIMARY in doc
