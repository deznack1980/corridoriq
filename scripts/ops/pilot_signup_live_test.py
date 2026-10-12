"""End-to-end contractor signup with real Postmark delivery (owner-approved), local server.

    python scripts/ops/pilot_signup_live_test.py                          # dry run, sends nothing
    python scripts/ops/pilot_signup_live_test.py --approved --to <mailbox>

Runs THIS checkout (the hotfix) on 127.0.0.1 with a disposable pilot database.
Live mode sends exactly one signup email through Postmark using the
POSTMARK_SERVER_TOKEN you set in this shell (never printed). Open the email on
this machine, click the link, and set the password the script prints on the
page that opens. The script detects the new account, then checks sign-in with
that password, the contractor session and portal access.

Dry run: in-memory mailer; the link is followed and the account completed
automatically.

The script prints MAIL_SENT / MAIL_DELIVERY_FAILED lines exactly as production
would (with Postmark's ErrorCode), so a rejected send identifies its cause.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PASSWORD = "Pilot-Live-Test-1"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to")
    parser.add_argument("--approved", action="store_true")
    parser.add_argument("--wait-minutes", type=float, default=15.0)
    args = parser.parse_args(argv)
    if args.approved != bool(args.to):
        parser.error("live mode needs both --approved and --to")
    live = bool(args.approved)
    if live and not (os.environ.get("POSTMARK_SERVER_TOKEN") or "").strip():
        parser.error("set POSTMARK_SERVER_TOKEN in this shell for the live run (it is never printed)")

    work = Path(tempfile.mkdtemp(prefix="ciq-pilot-live-"))
    (work / "pilot").mkdir()
    os.environ.update({
        "CORRIDORIQ_ENV": "test", "CORRIDORIQ_CACHE_WARMER": "0", "CORRIDORIQ_DATA_DIR": str(work),
        "CORRIDORIQ_DB_PATH": str(work / "db" / "corridoriq.db"), "CORRIDORIQ_PILOT_ROOT": str(work / "pilot"),
        "CORRIDORIQ_EMAIL_PROVIDER": "postmark" if live else "sink",
    })
    sys.path.insert(0, str(REPO))
    from http.server import ThreadingHTTPServer
    from pipeline.api import server as server_mod
    from pipeline.auth import mail
    from pipeline.db.database import init_db
    from pipeline.pilot.platform import Platform

    init_db()
    Platform(os.environ["CORRIDORIQ_PILOT_ROOT"]).connect().close()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    os.environ["CORRIDORIQ_PUBLIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cookie = {"v": None}

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        h = {"Content-Type": "application/json"}
        if cookie["v"]:
            h["Cookie"] = cookie["v"]
        c.request(method, path, json.dumps(body) if body is not None else None, h)
        r = c.getresponse()
        raw = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie" and v.startswith("corridoriq_pilot_session="):
                val = v.split(";", 1)[0]
                cookie["v"] = None if val.endswith("=") else val
        c.close()
        try:
            return r.status, json.loads(raw or b"{}")
        except Exception:
            return r.status, {}

    email = (args.to if live else "pilot-dry-run@example.test").strip().lower()
    checks = []
    try:
        status, body = call("POST", "/api/pilot/auth/signup", {"name": "Release Gate Test",
                            "business_name": "CorridorIQ Signup Test", "email": email, "language": "en",
                            "visit_id": "v-live-test"})
        checks.append(("signup accepted", status == 202 and body == {"status": "check_email"}))
        pdb = os.path.join(os.environ["CORRIDORIQ_PILOT_ROOT"], "platform.db")
        conn = sqlite3.connect(pdb)
        row = conn.execute("SELECT details_json FROM security_audit_log WHERE event_type="
                           "'email_verification_requested' ORDER BY id DESC LIMIT 1").fetchone()
        conn.close()
        result = json.loads(row[0] or "{}") if row else {}
        checks.append(("email handed to provider (audit result 'sent')", result.get("result") == "sent"))
        if result.get("result") != "sent":
            print("mail_error:", result.get("mail_error"))
            return _finish(checks, srv, work)
        if not live:
            link = mail.sink().outbox(to=email, kind=mail.PILOT_SIGNUP)[-1].link
            status, _ = call("POST", "/api/pilot/auth/complete-signup",
                             {"token": link.split("#t=", 1)[1], "password": PASSWORD})
            checks.append(("link completes the account", status == 201))
        else:
            print(f"Sent one signup email to {email}. Open it ON THIS MACHINE, click the link,")
            print(f"and set the password {PASSWORD!r} on the page that opens.")
            deadline, done = time.time() + args.wait_minutes * 60, False
            while time.time() < deadline and not done:
                c = sqlite3.connect(pdb)
                done = bool(c.execute("SELECT 1 FROM users WHERE normalized_email=?", (email,)).fetchone())
                c.close()
                time.sleep(0 if done else 3)
            checks.append(("real email received and the link created the account", done))
            if not done:
                return _finish(checks, srv, work)
        cookie["v"] = None
        status, _ = call("POST", "/api/pilot/auth/login", {"email": email, "password": PASSWORD})
        checks.append(("sign-in with the new account", status == 200))
        status, _ = call("GET", "/api/pilot/me")
        checks.append(("contractor session works", status == 200))
        status, _ = call("POST", "/api/pilot/contractor/requests", {"title": "Live test", "reference": "LT-1",
                         "lines": [{"raw_description": "test line", "quantity": 1, "uom": "ea"}]})
        checks.append(("contractor portal access (create a request)", status == 201))
        return _finish(checks, srv, work)
    except Exception as exc:
        checks.append((f"test ran ({type(exc).__name__})", False))
        return _finish(checks, srv, work)


def _finish(checks, srv, work):
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    return 0 if checks and all(ok for _, ok in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
