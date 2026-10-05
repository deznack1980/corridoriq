"""Option-B (P0) safety checks for the redesigned portal, public site, and the
procurement preview.

What these guard:
  * supplier screens render priority bands, never the underlying score;
  * the procurement preview's sample data cannot reach production pages,
    Python code, the API, or the database;
  * no hard-coded prices exist outside the sample-data file;
  * static serving stays an allowlist (no source, data, or legacy pages);
  * the dashboards still build the map and the list from one shared feed;
  * a non-relevant account (stand-in for the ToyotaLift case) stays off
    promoted surfaces as rendered in a browser;
  * the preview is administrator-only, and clicking through it sends nothing.
"""

from __future__ import annotations

import http.client
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.api import server as server_mod
from pipeline.api.server import static_target
from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import SCHEMA_PATH

REPO = Path(__file__).resolve().parents[2]

PREVIEW_PAGES = {
    "contractor-dashboard", "contractor-bom", "contractor-rfq",
    "contractor-rfqs", "contractor-quotes", "rfq-inbox",
}
SAMPLE_FILE = "demo/rfq-sample-data.js"


def _read(name: str) -> str:
    return (REPO / name).read_text(encoding="utf-8")


def _served_top_level(suffixes=(".html", ".js", ".css")) -> list[Path]:
    """Top-level files the server would actually serve."""
    out = []
    for p in sorted(REPO.iterdir()):
        if p.is_file() and p.suffix in suffixes:
            target, _ = static_target("/" + p.name)
            if target is not None and target == p.resolve():
                out.append(p)
    return out


def _portal_scripts() -> list[Path]:
    """Scripts that render inside the authenticated portal shell."""
    scripts = [REPO / "portal-common.js", REPO / "map-workspace.js"]
    for html in _served_top_level((".html",)):
        if "portal-common.js" in html.read_text(encoding="utf-8"):
            js = html.with_suffix(".js")
            if js.exists():
                scripts.append(js)
    return scripts


# ---------------------------------------------------------------------------
# 1. No raw scores rendered to supplier users
# ---------------------------------------------------------------------------

_SCORE_RENDER = [
    re.compile(r"Math\.round\([^)]*score", re.I),
    re.compile(r"score[\w.]*\)?\.toFixed\(", re.I),
    re.compile(r"\$\{\s*[\w.]*_score\s*\}"),
    re.compile(r"Score:</span>"),
]


@pytest.mark.parametrize("path", _portal_scripts(), ids=lambda p: p.name)
def test_portal_scripts_never_render_a_raw_score(path):
    text = path.read_text(encoding="utf-8")
    for rx in _SCORE_RENDER:
        assert not rx.search(text), f"{path.name}: raw score rendered ({rx.pattern})"


def test_score_filter_offers_bands_not_numbers():
    html = _read("opportunities.html")
    labels = re.findall(r'<select id="fscore">(.*?)</select>', html, re.S)[0]
    assert "High priority" in labels and not re.search(r">\s*\d+\+?\s*<", labels)


def test_band_helper_is_label_only():
    js = _read("portal-common.js")
    body = js[js.index("CIQ.bandBadge = function"):]
    body = body[: body.index("};")]
    assert "score" not in body.split("const b = CIQ.oppBand(score);")[1]
    assert "CIQ.scoreChip = (v) => CIQ.bandBadge(v);" in js


# ---------------------------------------------------------------------------
# 2. Demo data cannot enter production code or data paths
# ---------------------------------------------------------------------------

def test_sample_data_file_is_marked_and_neutral():
    text = _read(SAMPLE_FILE)
    assert "SAMPLE DATA — DEMONSTRATION ONLY" in text
    names = re.findall(r'name: "([^"]+)"', text)
    suppliers = [n for n in names if n.startswith("Sample Supplier")]
    assert suppliers == ["Sample Supplier A", "Sample Supplier B", "Sample Supplier C"]
    # Every named party is an obvious placeholder; no real business is named.
    for n in names:
        assert n.startswith("Sample"), n
    assert not re.search(r"sonoran|ferguson|hajoca|winsupply|toyota", text, re.I)


def test_only_preview_pages_load_demo_assets():
    for html in _served_top_level((".html",)):
        text = html.read_text(encoding="utf-8")
        loads_demo = "demo/" in text
        assert loads_demo == (html.stem in PREVIEW_PAGES), html.name


@pytest.mark.parametrize("stem", sorted(PREVIEW_PAGES))
def test_preview_pages_are_admin_only_and_bannered(stem):
    html, js = _read(stem + ".html"), _read(stem + ".js")
    assert SAMPLE_FILE in html and "Sample" in html.split("<title>")[1].split("</title>")[0]
    assert 'CIQ.guard("admin.system"' in js
    assert "sample: true" in js


_NETWORK = re.compile(r"CIQ\.api\.|fetch\(|XMLHttpRequest|sendBeacon|WebSocket|localStorage|EventSource")


@pytest.mark.parametrize("path", [*(f"{s}.js" for s in sorted(PREVIEW_PAGES)),
                                  "demo/rfq-sample-data.js", "demo/procurement-preview.js"])
def test_preview_code_makes_no_network_calls(path):
    assert not _NETWORK.search(_read(path)), path


def test_no_python_or_shared_runtime_references_sample_data():
    for py in (REPO / "pipeline").rglob("*.py"):
        if py.name == Path(__file__).name:
            continue
        text = py.read_text(encoding="utf-8", errors="ignore")
        assert "rfq-sample-data" not in text and "CIQ_SAMPLE" not in text, py
    for name in ("portal-common.js", "map-workspace.js", "site.js"):
        assert "CIQ_SAMPLE" not in _read(name) and "CIQP" not in _read(name), name


# ---------------------------------------------------------------------------
# 3. No made-up pricing outside the sample-data file
# ---------------------------------------------------------------------------

_PRICE = re.compile(r"\$\s?\d")
# The single approved commercial price (Contractor Pro), allowed only on the public
# contractor page. Every other price literal in a served page is still made-up pricing.
_APPROVED_PRICES = {"for-contractors.html": "$99/month"}


def test_no_price_literals_in_served_pages():
    for p in _served_top_level():
        text = p.read_text(encoding="utf-8")
        approved = _APPROVED_PRICES.get(p.name)
        if approved:
            assert approved in text, p.name
            text = text.replace(approved, "")
        hits = [m.group(0) for m in _PRICE.finditer(text)]
        assert not hits, f"{p.name} contains price literal(s): {hits[:3]}"
    assert not _PRICE.search(_read("demo/procurement-preview.js"))


def test_sample_prices_live_only_in_sample_file():
    text = _read(SAMPLE_FILE)
    assert "quotes:" in text and "Illustrative sample quotes" in text


# ---------------------------------------------------------------------------
# 4. Static asset serving stays restricted
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("path,expect", [
    ("/", "landing.html"),
    ("/index.html", "landing.html"),
    ("/login.html", "login.html"),
    ("/portal.css", "portal.css"),
    ("/assets/fonts/inter-latin-wght-normal.woff2", "inter-latin-wght-normal.woff2"),
    ("/assets/brand/favicon.svg", "favicon.svg"),
    ("/assets/vendor/leaflet-1.9.4/leaflet.js", "leaflet.js"),
    ("/demo/rfq-sample-data.js", "rfq-sample-data.js"),
])
def test_static_allowed(path, expect):
    target, ctype = static_target(path)
    assert target is not None and target.name == expect, path
    assert ctype


@pytest.mark.parametrize("path", [
    "/pipeline/api/server.py", "/pipeline/db/schema.sql", "/pipeline/config/settings.py",
    "/README.md", "/CorridorIQHQ.bat", "/tmp_recount.py", "/srv_out.log",
    "/data/demo/README.md", "/reports/demo/sample_arizona_industrial_report.md",
    "/docs/data-platform/EXECUTIVE_SUMMARY.md", "/agents/ceo/config.example.env",
    "/.git/config", "/.cursor/settings.json", "/assets/.hidden",
    "/../secret.txt", "/assets/../pipeline/config/settings.py", "/%2e%2e/README.md",
    "/assets%2f..%2fpipeline%2fapi%2fserver.py", "/demo/../pipeline/run.py",
    "/demo/notes.html", "/dashboard.html", "/companies.js", "/style.css", "/knowledge.html",
    "/pipeline/tests/test_option_b_safety.py", "/assets/corridoriq.ico.py",
])
def test_static_denied(path):
    target, _ = static_target(path)
    assert target is None, path


def test_static_never_serves_a_database_file():
    for p in REPO.rglob("*.db"):
        rel = "/" + p.relative_to(REPO).as_posix()
        assert static_target(rel)[0] is None, rel


# ---------------------------------------------------------------------------
# 5. Map/list consistency + shared scan stay wired the same way
# ---------------------------------------------------------------------------

def test_map_workspace_still_makes_exactly_the_two_shared_calls():
    mw = _read("map-workspace.js")
    assert mw.count("CIQ.api.get(") == 2
    assert '"/api/sales/opportunities?" + params({ page_size: FEED_SIZE })' in mw
    assert '"/api/sales/opportunities/map?" + params()' in mw
    for page in ("sales-dashboard.js", "admin-dashboard.js"):
        js = _read(page)
        assert js.count("CIQ.mapWorkspace(") == 1, page
        assert "/api/sales/opportunities" not in js, page  # no second, separate feed


def test_contact_rendering_still_gates_on_verified():
    mw, opp = _read("map-workspace.js"), _read("opportunities.js")
    assert 'if (!c || c.status !== "VERIFIED") return' in mw
    assert 'if (c.status !== "VERIFIED") return' in opp
    common = _read("portal-common.js")
    assert 'if (!vc || vc.status !== "VERIFIED")' in common
    assert 'if (c.status !== "VERIFIED") return { label: c.label || "Contact not yet verified."' in common


# ---------------------------------------------------------------------------
# 6. Browser checks against a real server + temporary database
# ---------------------------------------------------------------------------

def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture()
def site(tmp_path, monkeypatch):
    """HTTP server over a temp DB with a relevant plumber and a non-relevant
    equipment dealer (the generic stand-in for the ToyotaLift case)."""
    from pipeline.crm import scan_cache
    from pipeline.crm import service as crm
    from pipeline.sales_lanes.store import seed_lanes
    from pipeline.trust import account_view as trust
    from pipeline.trust.evidence import LANE_VERSION

    db_file = tmp_path / "optionb.db"

    def factory():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        cc.execute("PRAGMA foreign_keys = ON")
        return cc

    c = factory()
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) VALUES ('phoenix_az','Phoenix','AZ','connected')")
    seed_auth(c)
    seed_lanes(c)
    org = c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    admin_id = auth.create_user(c, organization_id=org, email="obadmin@corridoriq.com", password="ObAdmin123",
                                role_names=["admin"], must_change_password=False)
    rep_id = auth.create_user(c, organization_id=org, email="obrep@corridoriq.com", password="ObRep123",
                              role_names=["sales_representative"], must_change_password=False)

    def company(name, lane, score):
        now = _now()
        cid = c.execute("INSERT INTO companies (normalized_name, display_name, city, state, lifecycle_state, "
                        "created_at, updated_at) VALUES (?,?,?,?,'active',?,?)",
                        (name.upper(), name, "PHOENIX", "AZ", now, now)).lastrowid
        c.execute("INSERT INTO company_intelligence (company_id, company_priority_tier, company_priority_score, "
                  "active_projects, highest_opportunity_score, model_version) VALUES (?,?,?,?,?,?)",
                  (cid, "High", score, 1, score, "test"))
        c.execute("INSERT INTO company_sales_lanes (company_id, lane_key, fit, presentable, model_version, created_at) "
                  "VALUES (?,?,?,?,?,?)", (cid, lane, "HIGH", 1, LANE_VERSION, now))
        return cid

    def project(cid, description, score, days_ago=3):
        d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
        now = _now()
        pid = c.execute("INSERT INTO permits (jurisdiction, permit_number, permit_type, description, job_address, "
                        "city, state, status, contractor_company_id, issued_date, first_seen_at, last_updated_at) "
                        "VALUES ('phoenix_az',?,?,?,'1 Main St','PHOENIX','AZ','issued',?,?,?,?)",
                        (f"OB{cid}-{days_ago}", "Building", description, cid, d, now, now)).lastrowid
        c.execute("INSERT INTO projects (permit_id, jurisdiction, contractor_company_id, project_category, "
                  "project_lifecycle, opportunity_score, opportunity_date, opportunity_timing) "
                  "VALUES (?, 'phoenix_az', ?, 'commercial', 'Permit Issued', ?, ?, 'immediate')",
                  (pid, cid, score, d))

    plumber = company("Saguaro Plumbing Works", "PLUMBING_CORE", 87.3)
    dealer = company("Desert Forklift Sales", "SPECIALTY_OTHER", 99.0)
    project(plumber, "Replace 50 gal water heater and repipe", 87.3)
    project(dealer, "Install 16 feet tall storage racking system", 99.0, days_ago=2)
    c.commit()
    admin_ctx = auth.build_user_context(c, c.execute("SELECT * FROM users WHERE id=?", (admin_id,)).fetchone())
    crm.assign_company(c, admin_ctx, plumber, rep_id)
    crm.assign_company(c, admin_ctx, dealer, rep_id)
    c.commit()
    c.close()
    trust.clear_cache()
    scan_cache.clear()

    monkeypatch.setattr(server_mod, "_factory", factory)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.1)
    yield {"base": f"http://127.0.0.1:{port}", "port": port, "db": db_file, "factory": factory}
    srv.shutdown()
    trust.clear_cache()
    scan_cache.clear()


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", path)
    r = conn.getresponse()
    body = r.read()
    headers = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    return r.status, headers, body


def test_http_public_root_and_assets(site):
    status, headers, body = _get(site["port"], "/")
    assert status == 200 and b"For Supply Houses" in body and b"For Contractors" in body
    assert headers["content-type"].startswith("text/html") and headers["x-frame-options"] == "DENY"
    status, headers, _ = _get(site["port"], "/assets/fonts/inter-latin-wght-normal.woff2")
    assert status == 200 and headers["content-type"] == "font/woff2"
    for path in ("/pipeline/api/server.py", "/dashboard.html", "/assets/../README.md"):
        assert _get(site["port"], path)[0] == 404, path


def _table_counts(factory) -> dict:
    c = factory()
    try:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        c.close()


@pytest.fixture()
def browser():
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("Chromium not installed for Playwright")
        yield b
        b.close()


def _signed_in(browser, site, email, password):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    page.goto(site["base"] + "/login.html")
    page.fill("#email", email)
    page.fill("#password", password)
    page.click("#loginBtn")
    page.wait_for_url(re.compile(r".*-dashboard\.html"), timeout=10000)
    return ctx, page


def test_rendered_supplier_screens_show_bands_and_hide_non_relevant(browser, site):
    ctx, page = _signed_in(browser, site, "obadmin@corridoriq.com", "ObAdmin123")
    page.wait_for_selector("#mapWorkspace .mw-item", timeout=15000)
    feed = page.inner_text("#mapWorkspace")
    assert "Saguaro Plumbing Works" in feed and "High priority" in feed
    assert "87" not in feed and "Score" not in feed
    assert "Desert Forklift Sales" not in feed  # off-focus account is not promoted
    assert "Desert Forklift Sales" not in page.inner_text("#todaysAccounts")

    page.goto(site["base"] + "/opportunities.html?context=organization")
    page.wait_for_selector("#list .company-card", timeout=15000)
    listing = page.inner_text("#list")
    assert "High priority" in listing and "87" not in listing
    assert "Desert Forklift Sales" not in listing
    ctx.close()

    ctx, page = _signed_in(browser, site, "obrep@corridoriq.com", "ObRep123")
    assert page.url.endswith("/sales-dashboard.html")
    page.wait_for_selector("#priority .company-card", timeout=15000)
    assert "Saguaro Plumbing Works" in page.inner_text("#priority")
    assert "Desert Forklift Sales" not in page.inner_text("#priority")
    assert "87" not in page.inner_text("#priority")
    ctx.close()


def test_preview_is_admin_only(browser, site):
    ctx, page = _signed_in(browser, site, "obrep@corridoriq.com", "ObRep123")
    nav = page.inner_text("#ciqSidebar")
    assert "RFQ Inbox" not in nav and "Contractor portal" not in nav
    assert page.locator("#rfqEntry").is_hidden()
    for stem in sorted(PREVIEW_PAGES):
        page.goto(f"{site['base']}/{stem}.html")
        page.wait_for_url(re.compile(r".*/sales-dashboard\.html"), timeout=10000)
        assert page.locator("#ciqSampleBanner").count() == 0
    ctx.close()


def test_preview_flow_sends_nothing_and_writes_nothing(browser, site):
    ctx, page = _signed_in(browser, site, "obadmin@corridoriq.com", "ObAdmin123")
    nav = page.inner_text("#ciqSidebar")
    assert "RFQ Inbox" in nav and "Contractor portal" in nav
    page.wait_for_load_state("networkidle")
    before = _table_counts(site["factory"])

    calls = []
    page.on("request", lambda r: calls.append((r.method, r.url)))

    def visit(stem):
        page.goto(f"{site['base']}/{stem}.html")
        page.wait_for_selector("#ciqSampleBanner", timeout=10000)
        assert "SAMPLE DATA" in page.inner_text("#ciqSampleBanner").upper()

    def confirm():
        page.click(".modal-foot [data-a='ok']")

    visit("contractor-dashboard")
    visit("contractor-bom")
    page.click("#bomSample")
    page.wait_for_selector(".bom-tbl tbody tr")
    assert page.locator(".bom-tbl tbody tr").count() == 14
    page.click("#bomContinue")
    page.wait_for_url(re.compile(r".*/contractor-rfq\.html"))
    page.wait_for_selector("#ciqSampleBanner")
    page.click("#rfqSend")
    confirm()
    page.wait_for_url(re.compile(r".*/contractor-rfqs\.html"))
    page.wait_for_selector("#ciqSampleBanner")
    visit("contractor-quotes")
    page.click("[data-select='B']")
    confirm()
    page.wait_for_selector(".sel-banner")
    assert "no order was placed" in page.inner_text(".sel-banner")
    visit("rfq-inbox")
    page.click("#ibFill")
    page.click("#ibSubmit")
    confirm()
    page.wait_for_selector(".done-box")

    api = [(m, u.split(site["base"], 1)[-1]) for m, u in calls if "/api/" in u]
    assert api and all(m == "GET" and u.startswith("/api/auth/me") for m, u in api), api
    assert not [c for c in calls if c[0] not in ("GET", "HEAD")], calls
    assert _table_counts(site["factory"]) == before
    ctx.close()


# ---------------------------------------------------------------------------
# 7. Public pages make no coverage / freshness / procurement overclaims
# ---------------------------------------------------------------------------

PUBLIC_PAGES = ("home.html", "for-suppliers.html", "for-contractors.html", "login.html")

_OVERCLAIMS = {
    "real-time data": r"real[- ]?time",
    "complete coverage": r"complete coverage|all (?:of )?(?:the )?phoenix(?:-area| metro)? permits|every permit in",
    "universal daily freshness": r"every morning|each morning|refreshed daily|updated daily|daily refresh|daily,? permit",
    "guaranteed collection schedule": r"on a schedule|collected on a schedule|scheduled refresh|collects new permit records",
    "listed live coverage": r"live coverage",
    "live inventory": r"inventory",
    "live supplier pricing": r"live (?:supplier )?pric|real-time pric|contractor-specific pric",
    "live RFQ participation": r"participating suppliers|onboarding .{0,60}suppliers|suppliers (?:are|have) (?:joined|signed)",
    "unconfirmed mailbox": r"hello@corridoriq\.pro",
    "competitor-overlap headline": r"who to call|know why to call|not guesses|honest uncertainty",
}


def _visible_text(name: str) -> str:
    html = _read(name)
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html)


@pytest.mark.parametrize("name", PUBLIC_PAGES)
@pytest.mark.parametrize("claim", sorted(_OVERCLAIMS))
def test_public_page_makes_no_overclaim(name, claim):
    text = _visible_text(name)
    m = re.search(_OVERCLAIMS[claim], text, re.I)
    assert not m, f"{name}: {claim}: …{text[max(0, m.start() - 60):m.end() + 60]}…"


def test_permit_records_are_described_as_published_not_scheduled():
    for name in ("home.html", "for-suppliers.html"):
        assert "published by participating jurisdictions" in _visible_text(name), name
    landing = (REPO / "landing.html").read_text(encoding="utf-8")
    assert "published by participating jurisdictions" in landing


def test_public_coverage_and_procurement_are_qualified():
    home, sup, con = (_visible_text(n) for n in ("home.html", "for-suppliers.html", "for-contractors.html"))
    for name, text in (("home.html", home), ("for-suppliers.html", sup)):
        assert re.search(r"vary by jurisdiction and source", text), name
    for name, text in (("home.html", home), ("for-contractors.html", con)):
        assert "early access" in text.lower(), name
    # No public page lists individual jurisdictions as live coverage.
    for name in PUBLIC_PAGES:
        assert not re.search(r"\b(Chandler|Goodyear|Gilbert|Buckeye|Tempe)\b", _visible_text(name)), name


def test_public_contact_address_is_the_confirmed_mailbox():
    for name in ("for-suppliers.html", "for-contractors.html"):
        mails = set(re.findall(r"[\w.+-]+@corridoriq\.pro", _read(name)))
        assert mails == {"archie@corridoriq.pro"}, (name, mails)
