"""Public landing page (landing.html): claims, assets, links and isolation.

The landing page is held to the same overclaim rules as the existing public
pages (test_option_b_safety, test_contractor_tiers_claims) plus rules specific
to its copy: no unshipped procurement capabilities, no supplier-partner claims,
no links into unfinished pilot routes, and only verifiable, credited imagery.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from pipeline.api.server import static_target
from pipeline.tests.test_contractor_tiers_claims import _FORBIDDEN, _affirmative
from pipeline.tests.test_option_b_safety import _OVERCLAIMS

REPO = Path(__file__).resolve().parents[2]
PAGE = "landing.html"


def _html() -> str:
    return (REPO / PAGE).read_text(encoding="utf-8")


def _text() -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", _html(), flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html.replace("&amp;", "&"))


@pytest.mark.parametrize("claim", sorted(_OVERCLAIMS))
def test_landing_makes_no_existing_overclaim(claim):
    m = re.search(_OVERCLAIMS[claim], _text(), re.I)
    assert not m, f"{claim}: …{_text()[max(0, m.start() - 60):m.end() + 60]}…"


@pytest.mark.parametrize("claim", sorted(_FORBIDDEN))
def test_landing_makes_no_tier_overclaim(claim):
    assert not _affirmative(_FORBIDDEN[claim], _text()), claim


_UNSHIPPED = {
    "automatic quoting": r"automat\w*\s+(?:quot|pric|purchas|order|match|normaliz)|instant quote",
    "supplier pricing feeds": r"(?:live|real[- ]?time|current|up[- ]to[- ]date)\s+(?:supplier\s+)?pric",
    "stock levels": r"stock levels?|in[- ]stock|on[- ]hand",
    "fulfilment / payment": r"fulfil\w*|checkout|pay (?:online|in)|payments?\b",
    "product matching": r"product[- ]match|match(?:es|ed)? (?:to )?(?:products|skus?)|\bsku",
    "AI claims": r"\bAI\b|artificial intelligence|machine learning|\bLLM\b",
    "guaranteed leads": r"guarantee",
    "national reach": r"nationwide|across the (?:us|country|nation)|all 50",
    "social proof": r"testimonial|trusted by|customers? (?:include|like)|used by \d|\d+\+? (?:suppliers|contractors|customers)",
    "Contractor Pro features": r"photo product finder|voice[- ]to|good / better / best|truck stock|warranty history",
    "proprietary government access": r"exclusive (?:data|access)|proprietary (?:government|permit|data) access|before anyone else",
    "file / photo ingestion": r"\bpdf\b|spreadsheet|upload|\bcsv\b|(?:photo|picture|image)s? of (?:a|your) (?:list|bom|material)",
    "price / availability comparison": r"compar\w*\s+(?:\w+\s+){0,2}(?:price|pricing|availability|quotes?)|(?:price|availability) comparison",
    "support promises": r"business hours|24/7|within \d+ (?:hours|minutes)|same[- ]day response|support team",
}


@pytest.mark.parametrize("claim", sorted(_UNSHIPPED))
def test_landing_does_not_advertise_unshipped_capabilities(claim):
    hits = _affirmative(_UNSHIPPED[claim], _text())
    assert not hits, (claim, hits)


def test_no_named_supplier_or_partnership():
    text = _text()
    assert not re.search(r"sonoran|ferguson|hajoca|winsupply|partner(?:ed|ship)?|endorse", text, re.I)


def test_procurement_is_labelled_early_access_and_bounded():
    html = _html()
    proc = html[html.index('id="procurement"'):html.index('id="phoenix"')]
    assert "Early access" in proc
    assert re.search(r"Quoting, pricing, availability and ordering stay between contractor and supplier", proc)
    assert re.search(r"goes only to the supplier the contractor selects", proc)
    net = html[html.index('id="network"'):html.index('id="intelligence"')]
    # Supplier-side request receipt and the whole contractor column carry the label.
    assert net.count("Early access") >= 2


def test_illustrative_content_is_labelled():
    html = _html()
    hero = html[:html.index('id="network"')]
    assert "Illustrative example" in hero and "not data about the building pictured" in hero
    assert 'class="ex">Example<' in html  # material request sheet


def test_coverage_is_qualified_and_geography_not_presented_as_coverage():
    text = _text()
    assert text.count("vary by jurisdiction and source") >= 2
    assert "Market labels show geography, not data coverage" in text


def test_no_pilot_routes_or_internal_pages_are_linked():
    html = _html()
    for bad in ("/join/", "contractor-join", "contractor-account", "contractor-home", "contractor-request",
                "supplier-inbox", "/api/", "rfq-inbox", "contractor-dashboard", "demo/"):
        assert bad not in html, bad


def _local_refs(html: str) -> set[str]:
    refs = set(re.findall(r'(?:src|href)="([^"#:]+)"', html))
    for srcset in re.findall(r'srcset="([^"]+)"', html):
        refs |= {part.strip().split(" ")[0] for part in srcset.split(",")}
    return {r for r in refs if r and not r.startswith(("http", "mailto", "/"))}


def test_every_local_reference_is_served():
    html = _html()
    css = (REPO / "landing.css").read_text(encoding="utf-8")
    refs = _local_refs(html) | set(re.findall(r'url\("([^"]+)"\)', css))
    refs = {r for r in refs if not r.startswith("data:")}
    assert refs, "no references found"
    for ref in sorted(refs):
        target, _ = static_target("/" + ref)
        assert target is not None and target.is_file(), ref
    for anchor in re.findall(r'href="#([\w-]+)"', html):
        assert f'id="{anchor}"' in html, anchor


def test_no_third_party_resources_are_loaded():
    html = _html()
    for tag in re.findall(r"<(?:script|link|img|source|iframe)\b[^>]*>", html):
        if 'rel="canonical"' in tag:  # metadata, never fetched
            continue
        assert not re.search(r'(?:src|href|srcset)="https?://', tag), tag
    css = (REPO / "landing.css").read_text(encoding="utf-8")
    assert "http" not in css and "@import" not in css
    js = (REPO / "landing.js").read_text(encoding="utf-8")
    assert not re.search(r"fetch\(|XMLHttpRequest|sendBeacon|https?://", js)


def test_contact_is_the_confirmed_mailbox():
    assert set(re.findall(r"[\w.+-]+@[\w.-]+\.\w+", _html())) == {"archie@corridoriq.pro"}


def _section(html: str, sid: str) -> str:
    start = html.index(f'id="{sid}"')
    end = html.find("<section", start)
    return html[start:end if end != -1 else html.index("</main>")]


def test_contact_section_has_exact_clickable_details_and_nothing_invented():
    html = _html()
    contact = _section(html, "contact")
    assert 'href="tel:+16027628316"' in contact and "(602) 762-8316" in contact
    assert 'href="mailto:archie@corridoriq.pro"' in contact and "archie@corridoriq.pro" in contact
    assert "Phoenix, Arizona" in contact
    text = re.sub(r"<[^>]+>", " ", contact)
    assert not re.search(r"\b(?:suite|ste\.?|street|st\.|ave(?:nue)?|road|blvd|\d{5})\b", text, re.I)  # no address
    assert not re.search(r"\b(?:hours|mon|monday|am|pm|response time|team)\b", text, re.I)
    assert set(re.findall(r'href="tel:([^"]+)"', html)) == {"+16027628316"}


def test_contact_is_in_navigation_and_primary_ctas_scroll_to_it():
    html = _html()
    nav = html[html.index('id="navLinks"'):html.index("</nav>")]
    assert '<a href="#contact">Contact</a>' in nav
    for label in ("Request Demo", "Request a Demo", "Join Early Access", "Request Early Access"):
        hrefs = re.findall(rf'href="([^"]+)"[^>]*>{label}<', html)
        assert hrefs and set(hrefs) == {"#contact"}, (label, hrefs)
    # mailto appears only where the visitor explicitly chooses the address.
    for m in re.finditer(r'<a [^>]*href="mailto:[^"]*"[^>]*>([^<]*)<', html):
        assert m.group(1).strip() == "archie@corridoriq.pro" or "cr-" in html[m.start():m.end() + 200], m.group(0)
    assert "?subject=" not in html


def test_no_coordinates_or_debug_annotations():
    assert not re.search(r"\d+(?:\.\d+)?\s*°", _html())
    assert not re.search(r"\bgraticule\b|class=\"ticks\"|class=\"rings\"", _html())


def test_images_have_alt_dimensions_and_credits():
    html = _html()
    for img in re.findall(r"<img\b[^>]*>", html):
        assert re.search(r'\salt="', img), img
        assert re.search(r'\swidth="\d+"', img) and re.search(r'\sheight="\d+"', img), img
    photos = {re.sub(r"-\d+\.webp$", "", Path(p).name) for p in re.findall(r"assets/landing/[\w-]+\.webp", html)}
    assert photos == {"phoenix-metro-construction-hero", "phoenix-metro-sitework-aerial", "phoenix-industrial-satellite"}
    credits = html[html.index('class="credits"'):]
    assert credits.count("CC BY-SA 3.0") == 1 and credits.count("CC BY-SA 4.0") == 1 and credits.count("CC BY 4.0") == 1
    for author in ("cygnusloop99", "Hunter Trick", "Umbra Lab, Inc."):
        assert author in credits, author
    assert "cropped" in credits  # the hero adaptation is disclosed
    for f in (REPO / "assets" / "landing").iterdir():
        assert f.stat().st_size < 350_000, f.name


def test_fonts_are_self_hosted_with_licenses():
    fonts = REPO / "assets" / "fonts"
    assert (fonts / "archivo-latin-wdth-wght.woff2").is_file() and (fonts / "OFL-Archivo.txt").is_file()
    assert "SIL Open Font License" in (fonts / "OFL-Archivo.txt").read_text(encoding="utf-8")


def test_semantics_and_accessibility_basics():
    html = _html()
    assert html.count("<h1") == 1 and '<html lang="en">' in html
    assert 'class="skip" href="#main"' in html and '<main id="main">' in html
    assert 'aria-expanded="false" aria-controls="navLinks"' in html
    css = (REPO / "landing.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in css and ":focus-visible" in css


def test_no_third_party_branded_photography():
    # The earlier hero/closing photos showed a general contractor's crane and fence signage.
    html = _html()
    assert not re.search(r"clayco|X_Phoenix_Phase|The_Ray,", html, re.I)
    for f in (REPO / "assets" / "landing").iterdir():
        assert "phoenix-construction-hero" not in f.name and "phoenix-construction-tower" not in f.name, f.name


def test_brand_mark_uses_the_graphite_amber_palette():
    html = _html()
    assert html.count('src="assets/brand/corridoriq-mark-amber.svg"') == 2
    svg = (REPO / "assets" / "brand" / "corridoriq-mark-amber.svg").read_text(encoding="utf-8").lower()
    assert "#e8952b" in svg and "#1a4eb0" not in svg and "#5fd3a0" not in svg


def test_share_metadata_is_complete_and_points_at_served_assets():
    html = _html()
    for prop in ("og:type", "og:url", "og:title", "og:description", "og:image", "og:image:alt", "og:site_name"):
        assert f'property="{prop}"' in html, prop
    for name in ("twitter:card", "twitter:title", "twitter:description", "twitter:image"):
        assert f'name="{name}"' in html, name
    images = set(re.findall(r'content="https://corridoriq\.pro/([^"]+\.png)"', html))
    assert images == {"assets/brand/corridoriq-social-1200x630.png"}
    for ref in images | {"assets/brand/corridoriq-touch-180.png"}:
        target, _ = static_target("/" + ref)
        assert target is not None and target.is_file(), ref
    assert '<link rel="apple-touch-icon"' in html


def test_landing_is_the_public_homepage_and_old_homepage_is_retired():
    for path in ("/", "/index.html", "/landing.html"):
        target, ctype = static_target(path)
        assert target is not None and target.name == "landing.html" and ctype.startswith("text/html"), path
    assert static_target("/home.html") == (None, None)
    html = _html()
    assert html.count('class="brand" href="/"') == 2


def test_linked_subpages_match_the_pilot_and_have_no_dead_anchors():
    landing = _html()
    for name in ("for-suppliers.html", "for-contractors.html"):
        html = (REPO / name).read_text(encoding="utf-8")
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))
        for anchor in re.findall(r'href="/#([\w-]+)"', html):
            assert f'id="{anchor}"' in landing, (name, anchor)
        assert "?subject=" not in html, name
        assert set(re.findall(r'href="tel:([^"]+)"', html)) == {"+16027628316"}, name
        assert not re.search(r"csv|upload a|side by side|compare supplier responses|awarded elsewhere|as each source updates", text, re.I), name
    con = (REPO / "for-contractors.html").read_text(encoding="utf-8")
    callout = con[con.index('id="early-access"'):con.index("</section>", con.index('id="early-access"'))]
    assert 'href="login.html"' not in callout  # the portal login is for employees, not contractors


def test_contractor_sign_in_reaches_the_contractor_account_not_staff_login():
    landing = _html()
    contractors = (REPO / "for-contractors.html").read_text(encoding="utf-8")
    suppliers = (REPO / "for-suppliers.html").read_text(encoding="utf-8")
    assert 'href="contractor-account.html?mode=signin"' in contractors
    assert 'href="login.html"' not in contractors
    assert 'href="for-contractors.html"' in landing
    assert 'href="login.html"' in landing  # staff portal login stays separate
    assert "contractor-account" not in landing
    assert 'href="login.html"' in suppliers
    assert 'href="contractor-account.html?mode=signin"' not in suppliers
    target, _ = static_target("/contractor-account.html")
    assert target is not None and target.is_file()
