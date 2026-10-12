"""Release gate C-4: CORRIDORIQ_CLIENT_IP_SOURCE=railway — attribution, spoofing, independence.

    python scripts/ops/client_ip_staging_check.py simulate
    python scripts/ops/client_ip_staging_check.py remote --base https://<staging>.up.railway.app --phase spoof
    python scripts/ops/client_ip_staging_check.py remote --base https://<staging>.up.railway.app --phase independent

simulate  Local emulation of Railway's documented edge: the app sees an internal
          hop (100.64.0.9) as the TCP peer and the edge overwrites X-Real-IP /
          X-Forwarded-For with the real client. Several simulated users, forged
          headers, both with railway mode on and (as a control) off.

remote    Black-box check against a NON-PRODUCTION Railway service running this
          branch with CORRIDORIQ_CLIENT_IP_SOURCE=railway:
            spoof        from network A: 11 forgot-password requests, each with
                         forged X-Real-IP / X-Forwarded-For / CF-Connecting-IP and a
                         random non-existent email. PASS = the 11th is 429. The
                         forged values did not create new buckets, and the limit is
                         per visitor (a shared hop would allow 250).
            independent  from network B (a different public IP) within 10 minutes:
                         PASS = 202 while network A is still throttled.
          Non-existent example.invalid addresses are used, so no email is sent.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import random
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
PRODUCTION_HOSTS = {"corridoriq.pro", "www.corridoriq.pro", "app.corridoriq.pro"}
FORGOT_LIMIT = 10  # throttle.PORTAL_LIMITS["forgot_ip"]


def _forged_headers():
    rnd = lambda: f"{random.randint(11, 99)}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}"  # noqa: E731
    return {"X-Real-IP": rnd(), "X-Forwarded-For": f"{rnd()}, {rnd()}", "CF-Connecting-IP": rnd()}


def _post(host, port, path, body, headers, use_tls):
    conn = (http.client.HTTPSConnection(host, port, timeout=20, context=ssl.create_default_context())
            if use_tls else http.client.HTTPConnection(host, port, timeout=20))
    h = {"Content-Type": "application/json", **headers}
    conn.request("POST", path, json.dumps(body), h)
    r = conn.getresponse()
    r.read()
    conn.close()
    return r.status


# ---- remote (staging) ------------------------------------------------------------------

def remote(base: str, phase: str) -> int:
    parts = urlsplit(base)
    if parts.hostname in PRODUCTION_HOSTS:
        raise SystemExit("refusing to run against production; use a non-production Railway service")
    tls = parts.scheme == "https"
    port = parts.port or (443 if tls else 80)
    statuses = []
    count = FORGOT_LIMIT + 1 if phase == "spoof" else 1
    for _ in range(count):
        email = f"c4-{uuid.uuid4().hex[:12]}@example.invalid"
        statuses.append(_post(parts.hostname, port, "/api/auth/forgot-password", {"email": email},
                              _forged_headers() if phase == "spoof" else {}, tls))
    print(json.dumps({"phase": phase, "statuses": statuses}))
    if phase == "spoof":
        ok = all(s == 202 for s in statuses[:-1]) and statuses[-1] == 429
        print("PASS" if ok else "FAIL",
              "- forged headers ignored and the limit is per visitor" if ok else
              "- expected 10 x 202 then 429 (if the 11th is 202, attribution is not active or spoofable)")
    else:
        ok = statuses == [202]
        print("PASS" if ok else "FAIL", "- a different visitor is not affected by network A's limit")
    return 0 if ok else 1


# ---- local simulation -----------------------------------------------------------------

def simulate() -> int:
    work = Path(tempfile.mkdtemp(prefix="ciq-c4-"))
    for d in ("pilot", "tenants"):
        (work / d).mkdir()
    os.environ.update({"CORRIDORIQ_ENV": "test", "CORRIDORIQ_CACHE_WARMER": "0",
                       "CORRIDORIQ_DATA_DIR": str(work), "CORRIDORIQ_DB_PATH": str(work / "db" / "c.db"),
                       "CORRIDORIQ_PILOT_ROOT": str(work / "pilot"), "CORRIDORIQ_TENANT_ROOT": str(work / "tenants"),
                       "CORRIDORIQ_EMAIL_PROVIDER": "sink"})
    sys.path.insert(0, str(REPO))
    from http.server import ThreadingHTTPServer
    from pipeline.api import server as server_mod
    from pipeline.auth import throttle
    from pipeline.db.database import init_db

    init_db()

    class RailwayEdgeHandler(server_mod.ApiHandler):
        """Emulates Railway's documented edge in front of the app."""
        def setup(self):
            super().setup()
            self.client_address = ("100.64.0.9", self.client_address[1])  # internal hop

        def parse_request(self):
            ok = super().parse_request()
            if ok:
                true_client = self.headers.get("X-Sim-True-Client")
                for name in ("X-Real-IP", "X-Forwarded-For", "X-Sim-True-Client"):
                    del self.headers[name]          # edge overwrites client copies
                if true_client:
                    self.headers["X-Real-IP"] = true_client
                    self.headers["X-Forwarded-For"] = true_client
            return ok

    srv = ThreadingHTTPServer(("127.0.0.1", 0), RailwayEdgeHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    checks = []

    def user(ip, spoof=True):
        h = {"X-Sim-True-Client": ip}
        if spoof:
            h.update(_forged_headers())
        return h

    def forgot(ip, spoof=True):
        return _post("127.0.0.1", port, "/api/auth/forgot-password",
                     {"email": f"c4-{uuid.uuid4().hex[:10]}@example.invalid"}, user(ip, spoof), False)

    def audit_ips():
        conn = sqlite3.connect(os.environ["CORRIDORIQ_DB_PATH"])
        rows = {r[0] for r in conn.execute(
            "SELECT DISTINCT ip_address FROM security_audit_log WHERE event_type='password_reset_requested'")}
        conn.close()
        return rows

    def clear_audit():
        conn = sqlite3.connect(os.environ["CORRIDORIQ_DB_PATH"])
        conn.execute("DELETE FROM security_audit_log")
        conn.commit()
        conn.close()

    try:
        # --- railway mode ON ---
        os.environ["CORRIDORIQ_CLIENT_IP_SOURCE"] = "railway"
        throttle.portal.clear()
        clear_audit()
        a = [forgot("8.8.8.8") for _ in range(FORGOT_LIMIT + 1)]
        checks.append(("railway on: visitor A with forged headers is limited at the 11th request",
                       all(s == 202 for s in a[:-1]) and a[-1] == 429, a))
        b = forgot("1.1.1.1")
        checks.append(("railway on: visitor B is independent of A's limit", b == 202, b))
        c = [forgot("9.9.9.9", spoof=False) for _ in range(3)]
        checks.append(("railway on: visitor C unaffected", all(s == 202 for s in c), c))
        ips = audit_ips()
        checks.append(("railway on: audit log records the real client addresses only",
                       ips == {"8.8.8.8", "1.1.1.1", "9.9.9.9"}, sorted(ips)))
        # login lockout independence: 60 failed logins by A, then B signs in? (B has no
        # account here; use per-IP block state instead)
        for _ in range(throttle.PORTAL_LIMITS["login_ip"]):
            throttle.portal.hit("login_ip", "8.8.8.8")
        checks.append(("railway on: A's login lockout does not block B",
                       throttle.portal.login_blocked("8.8.8.8", None)
                       and not throttle.portal.login_blocked("1.1.1.1", None), None))
        # --- control: railway mode OFF (production default) ---
        os.environ.pop("CORRIDORIQ_CLIENT_IP_SOURCE", None)
        throttle.portal.clear()
        clear_audit()
        a = [forgot("8.8.8.8") for _ in range(FORGOT_LIMIT + 1)]
        checks.append(("control (mode off): every visitor shares the hop; shared limit is not hit at 11",
                       all(s == 202 for s in a), a))
        ips = audit_ips()
        checks.append(("control (mode off): audit log records only the shared hop", ips == {"100.64.0.9"},
                       sorted(ips)))
    finally:
        srv.shutdown()
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    for name, ok, detail in checks:
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  [{detail}]"))
    return 0 if all(ok for _, ok, _ in checks) else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("simulate")
    p = sub.add_parser("remote")
    p.add_argument("--base", required=True)
    p.add_argument("--phase", required=True, choices=["spoof", "independent"])
    args = parser.parse_args(argv)
    return simulate() if args.cmd == "simulate" else remote(args.base, args.phase)


if __name__ == "__main__":
    sys.exit(main())
