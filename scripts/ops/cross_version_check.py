"""Cross-version compatibility and rollback check (synthetic data only).

    python scripts/ops/cross_version_check.py [--production-ref fcf50fe] [--keep]

Phase A  production code (``git archive <ref>``) creates the databases with its
         own migrations and seeds: employees, live sessions, an outstanding
         password-reset token, a pilot supplier and a pilot contractor with a
         sent material request.
Phase B  this checkout's code opens the same databases (applying its additive
         migrations) and must keep every phase-A credential, session and
         token working while the new onboarding flows work end to end.
Phase C  rollback: production code reopens the databases after phase B and
         must still start and serve existing and newly created accounts.

Every phase drives the real HTTP handler of its code version. All paths are
temporary; no production data, credentials or network services are used.
Exit status is non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

COMMON = r'''
import http.client, json, os, sqlite3, sys, threading, time
sys.path.insert(0, os.getcwd())
from http.server import ThreadingHTTPServer
STATE = os.environ["XV_STATE"]
RESULTS = []

def check(name, ok, detail=""):
    RESULTS.append({"check": name, "ok": bool(ok), "detail": str(detail)[:200]})

def load():
    with open(STATE, encoding="utf-8") as fh:
        return json.load(fh)

def save(state):
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)

def serve():
    from pipeline.api import server as server_mod
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(0.05)
    return srv, srv.server_address[1]

class Client:
    def __init__(self, port, cookie=None):
        self.port, self.cookie = port, cookie
    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        headers = {"Content-Type": "application/json"}
        if self.cookie:
            headers["Cookie"] = self.cookie
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse(); raw = r.read()
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                val = v.split(";", 1)[0]
                self.cookie = None if val.endswith("=") else val
        c.close()
        try:
            return r.status, json.loads(raw or b"{}")
        except Exception:
            return r.status, raw.decode("utf-8", "replace")

def sink_token(email, kind):
    from pipeline.auth import mail
    msgs = mail.sink().outbox(to=email, kind=kind)
    if not msgs:
        return None
    link = msgs[-1].link or ""
    return link.split("#t=", 1)[1] if "#t=" in link else None

def finish():
    print("XV-RESULTS " + json.dumps(RESULTS))
'''

PHASE_A = COMMON + r'''
from pipeline.db.database import get_connection, init_db
from pipeline.auth import mail, service as auth
from pipeline.auth.seed import ensure_default_organization
from pipeline.pilot import accounts
from pipeline.pilot.platform import Platform

init_db()
conn = get_connection()
org = ensure_default_organization(conn)
auth.create_user(conn, organization_id=org, email="xv-admin@example.test", password="Admin-Pass-1",
                 role_names=["admin"], must_change_password=False)
auth.create_user(conn, organization_id=org, email="xv-rep@example.test", password="Rep-Pass-12",
                 role_names=["sales_representative"], must_change_password=False)
conn.commit(); conn.close()

root = os.environ["CORRIDORIQ_PILOT_ROOT"]
platform = Platform(root); pconn = platform.connect()
accounts.create_supplier(platform, "xv-supplier", "Synthetic Supply XV")
code = accounts.create_referral_code(pconn, platform, "xv-supplier", "xvsup", "counter")
accounts.add_supplier_user(pconn, platform, "xv-supplier", "xv-inbox@example.test", "Inbox-Pass-1", "Inbox XV")
pconn.close()

srv, port = serve()
admin = Client(port); s, _ = admin.call("POST", "/api/auth/login", {"email": "xv-admin@example.test", "password": "Admin-Pass-1"})
check("A: production admin login", s == 200, s)
rep = Client(port); s, _ = rep.call("POST", "/api/auth/login", {"email": "xv-rep@example.test", "password": "Rep-Pass-12"})
check("A: production rep login", s == 200, s)
s, _ = Client(port).call("POST", "/api/auth/forgot", {"email": "xv-rep@example.test"})
reset = sink_token("xv-rep@example.test", mail.PORTAL_RESET)
check("A: production reset token issued", s == 202 and reset, s)

pc = Client(port)
s, _ = pc.call("POST", "/api/pilot/auth/signup", {"name": "Pat Pilot", "business_name": "Pilot Plumbing XV",
               "email": "xv-pilot@example.test", "language": "en", "visit_id": "v-xv", "referral_code": code})
raw = sink_token("xv-pilot@example.test", mail.PILOT_SIGNUP)
s2, me = pc.call("POST", "/api/pilot/auth/complete-signup", {"token": raw, "password": "Pilot-Pass-1"})
check("A: production pilot contractor signup", s == 202 and s2 == 201, (s, s2))
conn_id = me["connections"][0]["connection_id"] if isinstance(me, dict) and me.get("connections") else None
s, req = pc.call("POST", "/api/pilot/contractor/requests", {"title": "XV rough-in", "reference": "XV-1",
                 "lines": [{"raw_description": "3/4in copper elbow", "quantity": 10, "uom": "ea"}]})
s2, _ = pc.call("POST", f"/api/pilot/contractor/requests/{req['request_id']}/send",
                {"connection_id": conn_id, "confirm": True})
check("A: production pilot request created and sent", s == 201 and s2 == 200, (s, s2))
srv.shutdown()
save({"admin_cookie": admin.cookie, "rep_cookie": rep.cookie, "rep_reset_token": reset,
      "pilot_cookie": pc.cookie, "pilot_request_id": req.get("request_id")})
finish()
'''

PHASE_B = COMMON + r'''
from pipeline.db.database import get_connection, init_db
from pipeline.auth import mail
from pipeline.pilot.platform import Platform

st = load()
init_db()                                    # integrated additive migrations on the production DB
Platform(os.environ["CORRIDORIQ_PILOT_ROOT"]).connect().close()
conn = get_connection()
cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
check("B: additive user columns present", {"account_kind", "account_state", "business_name", "business_category"} <= cols, sorted(cols))
kinds = {r[0] for r in conn.execute("SELECT DISTINCT account_kind FROM users")}
check("B: existing users default to employee", kinds == {"employee"}, kinds)
conn.close()

srv, port = serve()
s, me = Client(port, st["rep_cookie"]).call("GET", "/api/auth/me")
check("B: production session still valid", s == 200 and me["user"]["account_kind"] == "employee", s)
admin = Client(port); s, _ = admin.call("POST", "/api/auth/login", {"email": "xv-admin@example.test", "password": "Admin-Pass-1"})
check("B: production password hash verifies", s == 200, s)
s, _ = Client(port).call("POST", "/api/auth/reset", {"token": st["rep_reset_token"], "password": "Rep-Pass-NEW-3"})
check("B: production reset token completes", s == 200, s)
s, _ = Client(port).call("POST", "/api/auth/login", {"email": "xv-rep@example.test", "password": "Rep-Pass-NEW-3"})
check("B: login with reset password", s == 200, s)
s, _ = Client(port, st["rep_cookie"]).call("GET", "/api/auth/me")
check("B: reset revoked the old session", s == 401, s)
s, _ = Client(port, st["rep_cookie"]).call("GET", "/api/sales/dashboard")
check("B: revoked session cannot reach CRM", s == 401, s)
s, me = Client(port, st["pilot_cookie"]).call("GET", "/api/pilot/me")
check("B: production pilot session still valid", s == 200, s)
s, reqs = Client(port, st["pilot_cookie"]).call("GET", "/api/pilot/contractor/requests")
check("B: production pilot request readable", s == 200 and any(r.get("request_id") == st["pilot_request_id"] for r in reqs.get("items", [])), s)
s, _ = Client(port).call("POST", "/api/pilot/auth/login", {"email": "xv-pilot@example.test", "password": "Pilot-Pass-1"})
check("B: production pilot login", s == 200, s)

con = Client(port)
s, body = con.call("POST", "/api/auth/register/contractor", {"name": "Nia Network", "business_name": "Network Builds",
                   "email": "xv-contractor@example.test", "password": "Contract-Pass-1"})
check("B: contractor registration", s == 201 and con.cookie, s)
s, _ = Client(port).call("POST", "/api/auth/verify-email", {"token": sink_token("xv-contractor@example.test", mail.PORTAL_VERIFY)})
check("B: contractor email verification", s == 200, s)
s, _ = con.call("GET", "/api/contractor/intelligence")
check("B: verified contractor feature unlocked", s == 200, s)
sup = Client(port)
s, _ = sup.call("POST", "/api/auth/register/supplier", {"name": "Sol Supply", "business_name": "Sol Supply Co",
                "email": "xv-supplier@example.test", "password": "Supplier-Pass-1", "phone": "602-555-0101",
                "business_category": "plumbing"})
check("B: supplier registration", s == 201, s)
s, _ = sup.call("GET", "/api/supplier/rfqs")
check("B: unverified supplier locked", s == 403, s)
Client(port).call("POST", "/api/auth/verify-email", {"token": sink_token("xv-supplier@example.test", mail.PORTAL_VERIFY)})
s, users = admin.call("GET", "/api/admin/users")
check("B: admin user list includes onboarding state", s == 200 and any(u.get("account_kind") == "supplier" for u in users["items"]), s)
sup_id = next(u["id"] for u in users["items"] if u["email"] == "xv-supplier@example.test")
s, _ = sup.call("GET", "/api/supplier/rfqs")
check("B: verified but unapproved supplier locked", s == 403, s)
s, _ = admin.call("POST", f"/api/admin/users/{sup_id}/approve-supplier", {})
check("B: admin approves supplier", s == 200, s)
s, _ = sup.call("GET", "/api/supplier/rfqs")
check("B: approved supplier unlocked", s == 200, s)
for who, client in (("contractor", con), ("supplier", sup)):
    for path in ("/api/sales/dashboard", "/api/admin/users", "/api/sales/companies", "/api/status/refresh"):
        s, _ = client.call("GET", path)
        check(f"B: {who} denied {path}", s == 403, s)
gone = Client(port)
gone.call("POST", "/api/auth/register/contractor", {"name": "Gone Gary", "business_name": "Gone Co",
          "email": "xv-gone@example.test", "password": "Gone-Pass-123"})
s, users = admin.call("GET", "/api/admin/users")
gone_id = next(u["id"] for u in users["items"] if u["email"] == "xv-gone@example.test")
s, _ = admin.call("POST", f"/api/admin/users/{gone_id}/suspend", {})
s2, _ = gone.call("GET", "/api/auth/me")
check("B: suspension ends the session", s == 200 and s2 == 401, (s, s2))
srv.shutdown()
st.update(admin_cookie_b=admin.cookie, contractor_cookie=con.cookie, supplier_cookie=sup.cookie)
save(st)
conn = get_connection()
check("B: database integrity", conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
finish()
'''

PHASE_C = COMMON + r'''
from pipeline.db.database import get_connection, init_db
from pipeline.pilot.platform import Platform

st = load()
try:
    init_db()                                # production code on the newer database
    Platform(os.environ["CORRIDORIQ_PILOT_ROOT"]).connect().close()
    check("C: production starts on the migrated database", True)
except Exception as exc:
    check("C: production starts on the migrated database", False, repr(exc))
    finish(); sys.exit(0)
srv, port = serve()
s, _ = Client(port, st["admin_cookie_b"]).call("GET", "/api/auth/me")
check("C: session created by integrated code still valid", s == 200, s)
s, _ = Client(port).call("POST", "/api/auth/login", {"email": "xv-rep@example.test", "password": "Rep-Pass-NEW-3"})
check("C: employee login (password reset under integrated code)", s == 200, s)
s, _ = Client(port).call("POST", "/api/auth/login", {"email": "xv-admin@example.test", "password": "Admin-Pass-1"})
check("C: admin login", s == 200, s)
s, _ = Client(port, st["pilot_cookie"]).call("GET", "/api/pilot/me")
check("C: pilot session still valid", s == 200, s)
s, _ = Client(port).call("POST", "/api/pilot/auth/login", {"email": "xv-pilot@example.test", "password": "Pilot-Pass-1"})
check("C: pilot login", s == 200, s)
for who, cookie in (("contractor", st["contractor_cookie"]), ("supplier", st["supplier_cookie"])):
    c = Client(port, cookie)
    s, _ = c.call("GET", "/api/auth/me")
    check(f"C: onboarding {who} session readable by production", s == 200, s)
    for path in ("/api/sales/dashboard", "/api/admin/users", "/api/sales/companies", "/api/products/search"):
        s, _ = c.call("GET", path)
        check(f"C: production still denies {who} {path}", s == 403, s)
s, _ = Client(port).call("POST", "/api/auth/login", {"email": "xv-gone@example.test", "password": "Gone-Pass-123"})
check("C: suspended account stays locked out under production", s == 401, s)
s, _ = Client(port).call("POST", "/api/auth/login", {"email": "xv-contractor@example.test", "password": "Contract-Pass-1"})
check("C: onboarding contractor can still sign in after rollback", s == 200, s)
srv.shutdown()
conn = get_connection()
check("C: database integrity", conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok")
finish()
'''


def export_tree(ref: str, dest: Path) -> None:
    data = subprocess.run(["git", "-C", str(REPO), "archive", "--format=tar", ref],
                          check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data") if sys.version_info >= (3, 12) else tar.extractall(dest)


def run_phase(name: str, code: str, code_root: Path, env: dict, work: Path) -> list[dict]:
    script = work / f"phase_{name}.py"
    script.write_text(code, encoding="utf-8")
    proc = subprocess.run([sys.executable, str(script)], cwd=code_root, env=env,
                          capture_output=True, text=True, timeout=900)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("XV-RESULTS ")), None)
    if line is None:
        tail = (proc.stderr or proc.stdout)[-1500:]
        return [{"check": f"{name}: phase completed", "ok": False, "detail": tail}]
    return json.loads(line[len("XV-RESULTS "):])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--production-ref", default="fcf50fe")
    parser.add_argument("--keep", action="store_true", help="keep the temporary directory")
    parser.add_argument("--json", type=Path, help="also write results to this file")
    args = parser.parse_args(argv)

    work = Path(tempfile.mkdtemp(prefix="ciq-xv-"))
    try:
        prod_root = work / "production"
        prod_root.mkdir()
        export_tree(args.production_ref, prod_root)
        data = work / "data"
        (data / "pilot").mkdir(parents=True)
        (data / "tenants").mkdir()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("CORRIDORIQ_", "POSTMARK", "STRIPE", "PYTHONPATH"))}
        env.update({
            "CORRIDORIQ_ENV": "test", "CORRIDORIQ_CACHE_WARMER": "0",
            "CORRIDORIQ_DATA_DIR": str(data), "CORRIDORIQ_DB_PATH": str(data / "db" / "corridoriq.db"),
            "CORRIDORIQ_PILOT_ROOT": str(data / "pilot"), "CORRIDORIQ_TENANT_ROOT": str(data / "tenants"),
            "XV_STATE": str(work / "state.json"),
        })
        results = []
        results += run_phase("A_production", PHASE_A, prod_root, env, work)
        results += run_phase("B_integrated", PHASE_B, REPO, env, work)
        results += run_phase("C_rollback_production", PHASE_C, prod_root, env, work)
    finally:
        if not args.keep:
            shutil.rmtree(work, ignore_errors=True)

    failed = [r for r in results if not r["ok"]]
    for r in results:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['check']}" + ("" if r["ok"] else f"  [{r['detail']}]"))
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed "
          f"(production ref {args.production_ref})")
    if args.json:
        args.json.write_text(json.dumps({"production_ref": args.production_ref, "results": results}, indent=2),
                             encoding="utf-8")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
