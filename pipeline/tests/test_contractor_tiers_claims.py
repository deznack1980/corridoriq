"""Public-site claims for Contractor Free and Contractor Pro.

Contractor Pro and its field toolkit (photo product finder, compatibility
assistance, …) are planned, not built; contractor procurement is an early-access
preview with sample data. These tests keep the public pages from advertising
otherwise, and keep the contractor-privacy / sponsored-seat promise in place.
They complement the overclaim tests in test_option_b_safety.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PUBLIC_PAGES = ("home.html", "for-suppliers.html", "for-contractors.html", "login.html")
STATES = {"early": "Early access", "planned": "Planned", "soon": "Coming soon"}


def _html(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _text(name: str) -> str:
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", _html(name), flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html.replace("&amp;", "&"))


_NEGATION = re.compile(r"\b(?:never|not|no|nothing|without|isn't|aren't|won't)\b", re.I)


def _affirmative(pattern: str, text: str) -> list[str]:
    """Matches of ``pattern`` not negated in the preceding few words or within the match."""
    hits = []
    for m in re.finditer(pattern, text, re.I):
        if not _NEGATION.search(text[max(0, m.start() - 50):m.end()]):
            hits.append(text[max(0, m.start() - 60):m.end() + 60])
    return hits


_FORBIDDEN = {
    "Pro described as live": r"contractor pro (?:is )?(?:now )?(?:available|live|launched)|available (?:today|now)|now available|generally available",
    "photo ID production-ready": r"(?:photo|vision|image|camera)[\w\s-]{0,40}(?:production[- ]ready|in production|is live|available)",
    "certain identification": r"instantly identif|identif\w* any (?:product|part)|always identif|accurately identif",
    "guarantee language": r"guarantee",
    "AI-determined compatibility": r"\bAI[- ](?:determined|verified|confirmed|checked|matched) compatib|compatib\w* (?:determined|verified|confirmed) by (?:AI|a photo|the photo)",
    "live stock / availability": r"in[- ]stock|stock levels|live availability|real[- ]time availability",
    "live account pricing": r"live (?:account )?pric|instant (?:account )?pric|your (?:account )?price,? (?:live|instantly)",
    "unapproved Pro price": r"\$\s?\d|/\s?mo(?:nth)?\b|per month",
}
# The one approved public price: Contractor Pro, $99/month, on the contractor page only.
APPROVED_PRICE = "$99/month"
APPROVED_PRICE_PAGE = "for-contractors.html"


@pytest.mark.parametrize("name", PUBLIC_PAGES)
@pytest.mark.parametrize("claim", sorted(_FORBIDDEN))
def test_public_page_does_not_advertise(name, claim):
    text = _text(name)
    if claim == "unapproved Pro price" and name == APPROVED_PRICE_PAGE:
        text = text.replace(APPROVED_PRICE, " ")  # any other price or "/month" wording still fails
    hits = _affirmative(_FORBIDDEN[claim], text)
    assert not hits, f"{name}: {claim}: {hits}"


@pytest.mark.parametrize("name", PUBLIC_PAGES)
def test_no_automatic_supplier_access_to_contractor_private_data(name):
    private = r"(?:jobs|photos|customers|properties|material lists|searches|activity|equipment history|truck stock)"
    patterns = (
        rf"suppliers? (?:can |will |may )?(?:see|view|access|receive)s? (?:the |your |a |their )?contractors?'? (?:private )?{private}",
        rf"(?:see|view|access) (?:your|their|contractors'?) (?:private )?{private}",
        r"sponsor\w*[^.]{0,60}\b(?:gives?|grants?|includes?|provides?|gets?)\b[^.]{0,30}\baccess\b",
    )
    for p in patterns:
        hits = _affirmative(p, _text(name))
        assert not hits, f"{name}: {hits}"


def test_sponsored_seat_privacy_promise_is_stated():
    con = _text("for-contractors.html")
    assert re.search(r"Sponsorship never gives that supplier access to your jobs, photos, customers, properties, "
                     r"material lists, equipment history, truck stock, searches, or activity", con)
    assert "only what you explicitly choose to send" in con
    sup = _text("for-suppliers.html")
    assert re.search(r"sees a material request only when a contractor sends it", sup)
    assert re.search(r"not part of any supplier product .{0,40}sponsors a contractor's Contractor Pro seat", sup)


def test_product_safety_principle_is_stated():
    con = _text("for-contractors.html")
    for line in ("Vision identifies a candidate.", "Specifications determine compatibility.",
                 "The supplier's system determines availability.", "Your supplier account determines price."):
        assert line in con, line
    assert "never settles compatibility on its own" in con
    assert re.search(r"Regulated and safety-sensitive equipment may always require confirmation", con)
    assert "Professional confirmation required" in con


def test_pro_preview_is_labelled_as_illustrative_and_unreleased():
    html = _html("for-contractors.html")
    pro = html[html.index('id="pro"'):html.index('id="compare"')]
    assert "Illustrative concept · not a working product" in pro
    assert "coming soon" in re.sub(r"<[^>]+>", " ", pro).lower()
    assert "planned, not built" in pro


def _comparison_rows():
    html = _html("for-contractors.html")
    body = html[html.index('<table class="cmp">'):html.index("</table>")]
    rows, group = [], None
    for m in re.finditer(r'<tr class="grp"><th[^>]*>(.*?)</th>|<tr><th scope="row">(.*?)</th>(.*?)</tr>', body, re.S):
        if m.group(1):
            group = m.group(1)
            continue
        free, pro = re.findall(r'<td class="(y|n)">', m.group(3))
        state = re.search(r'<span class="state (\w+)">([^<]+)</span>', m.group(3))
        rows.append({"group": group, "name": m.group(2), "free": free, "pro": pro,
                     "state": state.group(1), "label": state.group(2)})
    return rows


def test_comparison_states_are_honest():
    rows = _comparison_rows()
    assert len(rows) == 17
    for r in rows:
        assert r["state"] in STATES and STATES[r["state"]] == r["label"], r
        if r["group"].startswith("Contractor Pro"):
            # Pro is not purchasable yet: every Pro row is Planned or Coming soon, never available.
            assert r["state"] in ("planned", "soon") and r["free"] == "n" and r["pro"] == "y", r
        else:
            assert r["state"] in ("early", "planned") and r["free"] == "y", r
    priority = [r for r in rows if r["name"].startswith("Priority Requests")]
    assert len(priority) == 1 and priority[0]["state"] == "soon"
    assert "no guaranteed response time" in priority[0]["name"]
    names = " | ".join(r["name"] for r in rows)
    for feature in ("Photo Product Finder", "Compatibility assistance", "Voice-to-material-list",
                    "Good / Better / Best", "Smart substitutions", "Truck stock tracking",
                    "Job-completion material checks", "warranty history"):
        assert feature in names, feature


@pytest.mark.parametrize("name", PUBLIC_PAGES)
def test_only_known_product_states_are_used(name):
    for cls in re.findall(r'class="state (\w+)"', _html(name)):
        assert cls in STATES, (name, cls)
    # Nothing contractor-facing is labelled as generally available.
    assert not re.search(r'class="state[^"]*"[^>]*>\s*Available', _html(name)), name


def test_pricing_is_free_and_unset():
    con = _text("for-contractors.html")
    assert "Contractor Free · no cost" in con and "No cost" in con
    # Approved price shown; Pro still not open for purchase; no outdated "not set" wording.
    assert "Contractor Pro is $99/month, month-to-month, and not yet open for purchase" in con
    assert "$99/month · month-to-month" in con and "$99/month · coming soon" in con
    assert "not been set" not in con and "pricing not set" not in con
    assert re.findall(r"\$\s?\d+(?:\.\d+)?", con) and set(re.findall(r"\$\s?\d+(?:\.\d+)?", con)) == {"$99"}
    assert "supplier response times are not guaranteed" in con


def test_homepage_routes_both_audiences_and_sells_the_system():
    html = _html("home.html")
    paths = html[html.index('<div class="paths">'):html.index('<div class="hero-note">')]
    assert 'href="for-suppliers.html"' in paths and 'href="for-contractors.html"' in paths
    text = _text("home.html")
    for phrase in ("Pays for intelligence", "Pays for productivity", "Procurement connects the two",
                   "Project signal", "Material need", "Quote / availability", "Contractor Pro"):
        assert phrase in text, phrase
    assert re.search(r"Steps 3–6 are contractor procurement, in early access", text)
