"""Capture Phase 2 launch screenshots against a throwaway staging database.

Never points at C:\\CorridorIQData. Run:

    python -m pipeline.reports.launch_screenshots
"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from pipeline.auth import mailer, onboarding, rate_limit
from pipeline.auth import service as auth
from pipeline.auth.seed import seed_auth
from pipeline.config import settings

OUT = Path(settings.PROJECT_ROOT) / "docs" / "operations" / "launch-screenshots"


def _seed(db_file: Path):
    c = sqlite3.connect(db_file)
    c.row_factory = sqlite3.Row
    c.executescript(settings.SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(c)
    org = c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]
    auth.create_user(
        c, organization_id=org, email="legacy@corridoriq.com",
        password="LegacyPass123", role_names=["sales_representative"],
        must_change_password=False,
    )
    auth.create_user(
        c, organization_id=org, email="onboard-admin@corridoriq.com",
        password="OnboardAdmin123", role_names=["admin"],
        must_change_password=False,
    )
    c.commit()
    c.close()


def main(argv=None) -> int:
    import argparse
    from playwright.sync_api import sync_playwright

    import pipeline.api.server as server_mod

    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args(argv)
    dest = Path(args.out)
    dest.mkdir(parents=True, exist_ok=True)
    mailer.clear_outbox()
    rate_limit.LIMITER.reset()

    tmp = Path(tempfile.mkdtemp(prefix="corridoriq-launch-"))
    db_file = tmp / "launch.db"
    _seed(db_file)

    def factory():
        cc = sqlite3.connect(db_file)
        cc.row_factory = sqlite3.Row
        cc.execute("PRAGMA foreign_keys = ON")
        return cc

    server_mod._factory = factory
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.15)
    origin = f"http://127.0.0.1:{port}"

    conn = factory()
    c_token, _ = onboarding.register_contractor(conn, {
        "name": "Alex Rivera",
        "business_name": "Rivera Plumbing",
        "email": "alex@riveraplumbing.test",
        "password": "SecurePass123",
    })
    s_token, _ = onboarding.register_supplier(conn, {
        "name": "Jordan Lee",
        "business_name": "Valley Pipe Supply",
        "email": "jordan@valleypipe.test",
        "password": "SecurePass123",
        "phone": "480-555-0199",
        "business_category": "plumbing",
    })
    conn.close()

    shots = [
        ("home-desktop", "/", 1440, 900),
        ("home-mobile-nav", "/", 390, 844, True),
        ("explore-desktop", "/explore.html", 1440, 900),
        ("register-contractor-desktop", "/register-contractor.html", 1440, 900),
        ("register-contractor-mobile", "/register-contractor.html", 390, 844),
        ("register-supplier-desktop", "/register-supplier.html", 1440, 900),
        ("register-supplier-mobile", "/register-supplier.html", 390, 844),
        ("login-desktop", "/login.html", 1440, 900),
        ("login-mobile", "/login.html", 390, 844),
        ("reset-password-desktop", "/reset-password.html", 1440, 900),
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for item in shots:
            name, path, w, h = item[0], item[1], item[2], item[3]
            open_nav = len(item) > 4 and item[4]
            page = browser.new_page(viewport={"width": w, "height": h})
            page.goto(origin + path, wait_until="networkidle")
            if open_nav:
                burger = page.locator(".site-burger")
                if burger.count():
                    burger.first.click()
                    page.wait_for_timeout(250)
            page.screenshot(path=str(dest / f"{name}.png"), full_page=True)
            page.close()

        for name, path, token, w, h in (
            ("contractor-welcome-desktop", "/contractor-welcome.html", c_token, 1440, 900),
            ("contractor-welcome-mobile", "/contractor-welcome.html", c_token, 390, 844),
            ("supplier-welcome-desktop", "/supplier-welcome.html", s_token, 1440, 900),
            ("supplier-welcome-mobile", "/supplier-welcome.html", s_token, 390, 844),
        ):
            context = browser.new_context(viewport={"width": w, "height": h})
            context.add_cookies([{
                "name": settings.SESSION_COOKIE_NAME,
                "value": token,
                "url": origin,
            }])
            page = context.new_page()
            page.goto(origin + path, wait_until="networkidle")
            page.wait_for_timeout(400)
            page.screenshot(path=str(dest / f"{name}.png"), full_page=True)
            context.close()
        browser.close()

    srv.shutdown()
    mailer.clear_outbox()
    print(f"wrote screenshots to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
