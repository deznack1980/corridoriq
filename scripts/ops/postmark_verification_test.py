"""Release gate C-2: a real verification email through Postmark, verified end to end.

    python scripts/ops/postmark_verification_test.py                         # dry run, sends nothing
    python scripts/ops/postmark_verification_test.py --approved --to owner@example.com

The integrated server runs on 127.0.0.1 with a disposable database. A synthetic
supplier account is registered with the given mailbox; the verification email
links to the local server, so clicking it on this machine completes the check.

Dry run (default): in-memory mailer; the link is followed automatically. Proves
the plumbing; sends nothing.

Live (--approved --to): CORRIDORIQ_EMAIL_PROVIDER=postmark with the server token
from the POSTMARK_SERVER_TOKEN environment variable you set for this run. The
token is never printed. Exactly one email is sent. The script then waits for
the link to be clicked and reports PASS when the account is verified.

After a live run, open the received message's source and confirm:
  Authentication-Results: spf=pass, dkim=pass (d=corridoriq.pro), dmarc=pass
  Return-Path: ...@pm-bounces.corridoriq.pro    From: CorridorIQ <noreply@corridoriq.pro>
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--to", help="owner-approved test mailbox (live mode)")
    parser.add_argument("--approved", action="store_true", help="owner approved sending one real email")
    parser.add_argument("--wait-minutes", type=float, default=15.0)
    args = parser.parse_args(argv)
    live = bool(args.approved and args.to)
    if args.approved != bool(args.to):
        parser.error("live mode needs both --approved and --to")
    if live and not (os.environ.get("POSTMARK_SERVER_TOKEN") or "").strip():
        parser.error("set POSTMARK_SERVER_TOKEN in this shell for the live run (it is never printed)")

    work = Path(tempfile.mkdtemp(prefix="ciq-c2-"))
    (work / "pilot").mkdir()
    (work / "tenants").mkdir()
    os.environ.update({
        "CORRIDORIQ_ENV": "test", "CORRIDORIQ_CACHE_WARMER": "0",
        "CORRIDORIQ_DATA_DIR": str(work), "CORRIDORIQ_DB_PATH": str(work / "db" / "corridoriq.db"),
        "CORRIDORIQ_PILOT_ROOT": str(work / "pilot"), "CORRIDORIQ_TENANT_ROOT": str(work / "tenants"),
        "CORRIDORIQ_EMAIL_PROVIDER": "postmark" if live else "sink",
    })
    sys.path.insert(0, str(REPO))
    from http.server import ThreadingHTTPServer
    from pipeline.api import server as server_mod
    from pipeline.auth import mail
    from pipeline.db.database import init_db

    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    port = srv.server_address[1]
    os.environ["CORRIDORIQ_PUBLIC_BASE_URL"] = f"http://127.0.0.1:{port}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        c.request(method, path, json.dumps(body) if body is not None else None,
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        data = json.loads(r.read() or b"{}")
        c.close()
        return r.status, data

    email = args.to if live else "c2-dry-run@example.test"
    result = {"mode": "live" if live else "dry-run", "provider": mail.provider_name(), "checks": []}
    try:
        status, body = call("POST", "/api/auth/register/supplier", {
            "name": "Release Gate Test", "business_name": "CorridorIQ C-2 Test", "email": email,
            "password": "C2-Test-Password-1", "phone": "602-555-0142", "business_category": "other"})
        sent = status == 201 and body.get("verification_sent") is True
        result["checks"].append({"check": "registration accepted and verification email handed to the provider",
                                 "ok": sent, "detail": status})
        if not sent:
            return _finish(result, srv, work, 1)
        if not live:
            link = mail.sink().outbox(to=email, kind=mail.PORTAL_VERIFY)[-1].link
            result["checks"].append({"check": "link uses the URL fragment, never the query string",
                                     "ok": "#t=" in link and "token=" not in link.split("#")[0]})
            status, _ = call("POST", "/api/auth/verify-email", {"token": link.split("#t=", 1)[1]})
            result["checks"].append({"check": "following the link verifies the account", "ok": status == 200})
        else:
            print(f"One verification email was sent to {email} via Postmark.")
            print("Open it ON THIS MACHINE and click the link (it targets the local test server).")
            deadline = time.time() + args.wait_minutes * 60
            verified = False
            while time.time() < deadline and not verified:
                conn = sqlite3.connect(os.environ["CORRIDORIQ_DB_PATH"])
                row = conn.execute("SELECT email_verified_at FROM users WHERE normalized_email=?",
                                   (email.strip().lower(),)).fetchone()
                conn.close()
                verified = bool(row and row[0])
                if not verified:
                    time.sleep(3)
            result["checks"].append({"check": "real email received and its link verified the account",
                                     "ok": verified})
            result["next"] = ("Check the message source: Authentication-Results spf=pass, dkim=pass "
                              "(d=corridoriq.pro), dmarc=pass; Return-Path @pm-bounces.corridoriq.pro.")
        return _finish(result, srv, work, 0 if all(c["ok"] for c in result["checks"]) else 1)
    except Exception as exc:  # never echo the token; exceptions carry no secret
        result["checks"].append({"check": "test ran", "ok": False, "detail": type(exc).__name__})
        return _finish(result, srv, work, 1)


def _finish(result, srv, work, code):
    srv.shutdown()
    shutil.rmtree(work, ignore_errors=True)
    for c in result["checks"]:
        print(("PASS " if c["ok"] else "FAIL ") + c["check"])
    if result.get("next"):
        print(result["next"])
    print(json.dumps({k: v for k, v in result.items() if k != "checks"}))
    return code


if __name__ == "__main__":
    sys.exit(main())
