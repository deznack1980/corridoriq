"""Contractor account foundation + single-supplier pilot.

Runs the real portal server (ApiHandler) against a temporary pilot root with
synthetic tenants and users. The application-database factory points at a
temporary billing database. A pilot route may read users, organizations, and
billing_accounts there to classify material-request priority. The SQL trace
fails the test if that lookup touches supplier intelligence tables.
"""

from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from pipeline.api import server as server_mod
from pipeline.config.settings import SCHEMA_PATH
from pipeline.pilot import accounts, analytics, api as pilot_api, materials, qr
from pipeline.tests.test_billing import PRICE
from pipeline.tests.test_billing_contractor_pro import PRICE_C, ccfg
from pipeline.pilot.__main__ import check_public_url, referral_url
from pipeline.pilot.platform import ENV_ROOT, PILOT_COOKIE, Platform, PilotNotConfigured, validate_root
from pipeline.tenancy.contractor import ContractorContext, ContractorStore, ContractorTenant
from pipeline.tenancy.model import TenantError

REPO = Path(__file__).resolve().parents[2]
PW = "Synthetic-Pass-123"


# ---------------------------------------------------------------- harness

class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = None
        self.staff_cookie = None
        self.last_pilot_cookie = None

    def call(self, method, path, body=None, *, ctype="application/json", headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = dict(headers or {})
        cookies = [c for c in (self.cookie, self.staff_cookie) if c]
        if cookies:
            headers["Cookie"] = "; ".join(cookies)
        data = None
        if body is not None:
            data = body if isinstance(body, (bytes, str)) else json.dumps(body)
            headers["Content-Type"] = ctype
        conn.request(method, path, body=data, headers=headers)
        res = conn.getresponse()
        raw = res.read()
        for h, v in res.getheaders():
            if h.lower() == "set-cookie" and v.startswith(PILOT_COOKIE + "="):
                self.last_pilot_cookie = v
                val = v.split(";", 1)[0]
                self.cookie = None if val.endswith("=") else val
        conn.close()
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = raw.decode("utf-8", "replace")
        return res.status, payload


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "pilot"
    monkeypatch.setenv(ENV_ROOT, str(root))
    billing_db = tmp_path / "billing.db"
    init = sqlite3.connect(billing_db)
    init.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    init.close()
    statements = []

    def factory():
        conn = sqlite3.connect(billing_db)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(server_mod, "_factory", factory)
    from pipeline.auth import mail, throttle
    mail.sink().clear()
    throttle.pilot.clear()
    throttle.portal.clear()
    pilot_api._hits.clear()
    platform = Platform(root)
    conn = platform.connect()
    accounts.create_supplier(platform, "supplier-a", "Synthetic Supply A")
    accounts.create_supplier(platform, "supplier-b", "Synthetic Supply B")
    code_a = accounts.create_referral_code(conn, platform, "supplier-a", "supa", "counter")
    code_b = accounts.create_referral_code(conn, platform, "supplier-b", "supb")
    accounts.add_supplier_user(conn, platform, "supplier-a", "inbox-a@example.test", PW, "Inbox A")
    accounts.add_supplier_user(conn, platform, "supplier-b", "inbox-b@example.test", PW, "Inbox B")
    conn.close()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield {"root": root, "platform": platform, "port": srv.server_address[1], "code_a": code_a,
           "code_b": code_b, "billing_db": billing_db}
    srv.shutdown()
    srv.server_close()
    # The priority lookup may read users, organizations, and billing_accounts only.
    for sql in statements:
        lowered = sql.lower()
        for banned in ("permits", "companies", "projects", "company_customer_priority", "source_health"):
            assert banned not in lowered, sql


def client(env) -> Client:
    return Client(env["port"])


def _sink_token(email, kind):
    from pipeline.auth import mail
    msgs = mail.sink().outbox(to=email, kind=kind)
    assert msgs, (email, kind)
    link = msgs[-1].link or ""
    assert "#t=" in link and "token=" not in (msgs[-1].link or "").split("#", 1)[0]
    return link.split("#t=", 1)[1]


def signup(env, email, business, *, code=None, utm=None, language="en", visit="v-test-visit-1"):
    from pipeline.auth import mail
    c = client(env)
    body = {"name": "Synthetic Person", "business_name": business, "email": email,
            "language": language, "visit_id": visit}
    if code:
        body["referral_code"] = code
    if utm:
        body["utm"] = utm
    status, resp = c.call("POST", "/api/pilot/auth/signup", body)
    assert status == 202 and resp == {"status": "check_email"}, resp
    assert c.cookie is None
    raw = _sink_token(email.lower(), mail.PILOT_SIGNUP)
    status, me = c.call("POST", "/api/pilot/auth/complete-signup", {"token": raw, "password": PW})
    assert status == 201, me
    return c, me


def login(env, email):
    c = client(env)
    status, me = c.call("POST", "/api/pilot/auth/login", {"email": email, "password": PW})
    assert status == 200, me
    return c, me


def new_request(c, title="Rough-in", lines=None):
    lines = lines or [{"raw_description": "1/2in copper 90 elbow, sweat", "quantity": 12, "uom": "ea"},
                      {"raw_description": "PEX-A 3/4in x 100ft coil", "quantity": 2, "note": "red"}]
    status, req = c.call("POST", "/api/pilot/contractor/requests", {"title": title, "reference": "Job 14", "lines": lines})
    assert status == 201, req
    return req


def send(c, req, connection_id):
    return c.call("POST", f"/api/pilot/contractor/requests/{req['request_id']}/send",
                  {"connection_id": connection_id, "confirm": True})


def db(env):
    conn = sqlite3.connect(env["root"] / "platform.db")
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture()
def world(env):
    a, me_a = signup(env, "owner-a@example.test", "Alpha Plumbing", code=env["code_a"],
                     utm={"utm_source": "supplier_a", "utm_medium": "qr", "utm_campaign": "counter_pilot"})
    b, me_b = signup(env, "owner-b@example.test", "Beta Mechanical", code=env["code_a"])
    sa, _ = login(env, "inbox-a@example.test")
    sb, _ = login(env, "inbox-b@example.test")
    return {"env": env, "a": a, "b": b, "sa": sa, "sb": sb, "me_a": me_a, "me_b": me_b,
            "conn_a": me_a["connections"][0]["connection_id"], "conn_b": me_b["connections"][0]["connection_id"]}


# ---------------------------------------------------------------- 1-2 contractor isolation

def test_contractor_cannot_read_another_contractors_request(world):
    req_b = new_request(world["b"], "Beta private job")
    status, _ = world["a"].call("GET", f"/api/pilot/contractor/requests/{req_b['request_id']}")
    assert status == 404
    status, items = world["a"].call("GET", "/api/pilot/contractor/requests")
    assert status == 200 and items["items"] == []
    status, mine = world["b"].call("GET", f"/api/pilot/contractor/requests/{req_b['request_id']}")
    assert status == 200 and mine["title"] == "Beta private job"


def test_contractor_cannot_modify_or_send_another_contractors_request(world):
    req_b = new_request(world["b"])
    rid = req_b["request_id"]
    status, _ = world["a"].call("PATCH", f"/api/pilot/contractor/requests/{rid}",
                                {"title": "hijack", "lines": [{"raw_description": "x", "quantity": 1}]})
    assert status == 404
    status, _ = send(world["a"], req_b, world["conn_a"])
    assert status == 404
    status, still = world["b"].call("GET", f"/api/pilot/contractor/requests/{rid}")
    assert still["title"] == "Rough-in" and still["status"] == "DRAFT"
    # A cannot send through B's connection either.
    mine = new_request(world["a"])
    status, err = send(world["a"], mine, world["conn_b"])
    assert status == 404 and err["error"] == "connection_not_found"


# ---------------------------------------------------------------- 3-6 supplier visibility

def test_supplier_cannot_see_unsent_request(world):
    new_request(world["a"], "Unsent draft")
    status, inbox = world["sa"].call("GET", "/api/pilot/supplier/requests")
    assert status == 200 and inbox["items"] == []


def test_send_requires_explicit_confirmation(world):
    req = new_request(world["a"])
    status, err = world["a"].call("POST", f"/api/pilot/contractor/requests/{req['request_id']}/send",
                                  {"connection_id": world["conn_a"]})
    assert status == 400 and err["error"] == "confirmation_required"
    assert world["sa"].call("GET", "/api/pilot/supplier/requests")[1]["items"] == []


def test_supplier_sees_only_the_request_sent_to_it(world):
    req = new_request(world["a"], "Sent job")
    status, sent = send(world["a"], req, world["conn_a"])
    assert status == 200 and sent["status"] == "SENT" and sent["sent_to_supplier"] == "Synthetic Supply A"
    status, inbox = world["sa"].call("GET", "/api/pilot/supplier/requests")
    assert [i["title"] for i in inbox["items"]] == ["Sent job"]
    item = inbox["items"][0]
    assert item["contractor_name"] == "Alpha Plumbing" and item["status"] == "NEW"
    status, detail = world["sa"].call("GET", f"/api/pilot/supplier/requests/{item['share_id']}")
    assert status == 200 and detail["status"] == "VIEWED"
    assert [(l["raw_description"], l["quantity"], l["uom"], l["note"]) for l in detail["lines"]] == [
        ("1/2in copper 90 elbow, sweat", 12.0, "ea", None), ("PEX-A 3/4in x 100ft coil", 2.0, None, "red")]
    status, acked = world["sa"].call("POST", f"/api/pilot/supplier/requests/{item['share_id']}/acknowledge", {})
    assert acked["status"] == "ACKNOWLEDGED"
    # The contractor sees the supplier's real actions — and nothing invented.
    status, mine = world["a"].call("GET", f"/api/pilot/contractor/requests/{req['request_id']}")
    assert mine["sent_to"][0]["status"] == "ACKNOWLEDGED"
    # Sent requests are frozen; resending is refused.
    assert world["a"].call("PATCH", f"/api/pilot/contractor/requests/{req['request_id']}",
                           {"title": "t", "lines": [{"raw_description": "x", "quantity": 1}]})[0] == 409
    assert send(world["a"], req, world["conn_a"])[0] == 409


def test_other_supplier_cannot_see_the_request(world):
    req = new_request(world["a"])
    send(world["a"], req, world["conn_a"])
    share_id = world["sa"].call("GET", "/api/pilot/supplier/requests")[1]["items"][0]["share_id"]
    assert world["sb"].call("GET", "/api/pilot/supplier/requests")[1]["items"] == []
    assert world["sb"].call("GET", f"/api/pilot/supplier/requests/{share_id}")[0] == 404
    assert world["sb"].call("POST", f"/api/pilot/supplier/requests/{share_id}/acknowledge", {})[0] == 404


def test_supplier_gains_no_other_contractor_data_after_receiving_one_request(world):
    sent = new_request(world["a"], "Shared")
    private = new_request(world["a"], "Private second job")
    send(world["a"], sent, world["conn_a"])
    sa = world["sa"]
    for method, path, body in [
        ("GET", "/api/pilot/contractor/requests", None),
        ("GET", f"/api/pilot/contractor/requests/{private['request_id']}", None),
        ("GET", f"/api/pilot/contractor/requests/{sent['request_id']}", None),
        ("GET", "/api/pilot/contractor/connections", None),
        ("PATCH", "/api/pilot/contractor/profile", {"business_name": "x"}),
        ("POST", "/api/pilot/contractor/requests", {"title": "x", "lines": [{"raw_description": "x", "quantity": 1}]}),
        ("POST", "/api/pilot/contractor/lines/parse", {"text": "1 x"}),
    ]:
        status, _ = sa.call(method, path, body)
        assert status == 403, (method, path, status)
    inbox = sa.call("GET", "/api/pilot/supplier/requests")[1]["items"]
    assert [i["title"] for i in inbox] == ["Shared"]
    detail = sa.call("GET", f"/api/pilot/supplier/requests/{inbox[0]['share_id']}")[1]
    allowed = {"share_id", "contractor_name", "title", "reference", "sent_at", "status", "priority",
               "viewed_at", "acknowledged_at", "lines"}
    assert set(detail) == allowed
    blob = json.dumps(detail)
    for leak in ("Private second job", "owner-a@example.test", "request_id", "tenant", "phone", "visit", "utm"):
        assert leak not in blob, leak
    me = sa.call("GET", "/api/pilot/me")[1]
    assert "contractor" not in me and "connections" not in me


def test_contractor_cannot_use_supplier_inbox(world):
    for path in ("/api/pilot/supplier/requests", "/api/pilot/supplier/requests/s-0000000000000000"):
        assert world["a"].call("GET", path)[0] == 403


def test_unpaid_material_request_is_standard_despite_a_forged_priority(env):
    c, me = signup(env, "std@example.test", "Standard Mechanical", code=env["code_a"])
    status, req = c.call("POST", "/api/pilot/contractor/requests", {
        "title": "Job",
        "lines": [{"raw_description": "pipe", "quantity": 1}],
        "priority": "priority",
        "organization_id": 99999,
        "plan": "contractor_pro",
        "entitlement": "contractor_pro",
        "account_type": "contractor",
    })
    assert status == 201 and req["priority"] == "standard"
    status, sent = send(c, req, me["connections"][0]["connection_id"])
    assert status == 200 and sent["priority"] == "standard"
    supplier, _ = login(env, "inbox-a@example.test")
    inbox = supplier.call("GET", "/api/pilot/supplier/requests")[1]["items"]
    detail = supplier.call("GET", f"/api/pilot/supplier/requests/{inbox[0]['share_id']}")[1]
    assert detail["priority"] == "standard"
    assert "organization" not in json.dumps(detail)


def _billing(env):
    conn = sqlite3.connect(env["billing_db"])
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _contractor_billing(env, email, *, state="active", price=PRICE_C, account_type="contractor",
                        with_account=True, verified=True, owner=True):
    """Local billing row for the same email. Not a Stripe call and not a second pilot user."""
    conn = _billing(env)
    now = "2026-10-05T12:00:00+00:00"
    slug = "bill-" + re.sub(r"[^a-z0-9]+", "-", email.lower()).strip("-")
    org = conn.execute(
        "INSERT INTO organizations (name, slug, is_active, account_type, created_at, updated_at) "
        "VALUES (?, ?, 1, ?, ?, ?)",
        (email, slug, account_type, now, now),
    ).lastrowid
    verified_at = now if verified else None
    verified_addr = email.lower() if verified else None
    uid = conn.execute(
        "INSERT INTO users (organization_id, email, normalized_email, password_hash, is_active, "
        "created_at, updated_at, email_verified_at, email_verified_address) "
        "VALUES (?, ?, ?, 'not-a-login', 1, ?, ?, ?, ?)",
        (org, email, email.lower(), now, now, verified_at, verified_addr),
    ).lastrowid
    if owner and account_type == "contractor":
        role = conn.execute("SELECT id FROM roles WHERE name='contractor_owner'").fetchone()
        if role is None:
            conn.execute(
                "INSERT INTO roles (name, display_name, is_system_role, created_at, updated_at) "
                "VALUES ('contractor_owner', 'Contractor owner', 1, ?, ?)", (now, now))
            role_id = conn.execute("SELECT id FROM roles WHERE name='contractor_owner'").fetchone()["id"]
        else:
            role_id = role["id"]
        conn.execute("INSERT INTO user_roles (user_id, role_id, assigned_at) VALUES (?, ?, ?)",
                     (uid, role_id, now))
    if with_account:
        conn.execute(
            """
            INSERT INTO billing_accounts (
                organization_id, stripe_customer_id, stripe_subscription_id, stripe_price_id,
                subscription_status, billing_state, livemode, current_period_end, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, '2099-01-01T00:00:00+00:00', ?, ?)
            """,
            (org, f"cus_TEST{org}", f"sub_TEST{org}", price, state, state, now, now),
        )
    conn.commit()
    conn.close()
    return org


def _use_test_billing(monkeypatch):
    monkeypatch.setattr("pipeline.billing.entitlements.load_config", lambda environ=None: ccfg())


def _request(c, **extra):
    body = {"title": "Job", "lines": [{"raw_description": "pipe", "quantity": 1}]}
    body.update(extra)
    return c.call("POST", "/api/pilot/contractor/requests", body)


def test_paid_contractor_pro_request_is_priority_and_reaches_the_supplier(env, monkeypatch):
    _use_test_billing(monkeypatch)
    email = "pro@example.test"
    org = _contractor_billing(env, email)
    c, me = signup(env, email, "Pro Mechanical", code=env["code_a"])
    status, req = _request(c, priority="standard", organization_id=99999, plan="founding_supply_partner",
                           entitlement="supplier_intelligence", account_type="supplier")
    assert status == 201 and req["priority"] == "priority"
    status, sent = send(c, req, me["connections"][0]["connection_id"])
    assert status == 200 and sent["priority"] == "priority"
    supplier, _ = login(env, "inbox-a@example.test")
    detail = supplier.call("GET", "/api/pilot/supplier/requests")[1]
    share = supplier.call("GET", f"/api/pilot/supplier/requests/{detail['items'][0]['share_id']}")[1]
    assert share["priority"] == "priority"
    assert org != 99999


def test_past_due_contractor_pro_new_request_is_standard(env, monkeypatch):
    _use_test_billing(monkeypatch)
    email = "lapse@example.test"
    org = _contractor_billing(env, email)
    c, me = signup(env, email, "Lapse Mechanical", code=env["code_a"])
    first = _request(c, title="While paid")[1]
    assert first["priority"] == "priority"
    conn = _billing(env)
    conn.execute("UPDATE billing_accounts SET billing_state='canceled', subscription_status='canceled' "
                 "WHERE organization_id=?", (org,))
    conn.commit()
    conn.close()
    second = _request(c, title="After lapse", priority="priority", organization_id=org)[1]
    assert second["priority"] == "standard"
    status, sent = send(c, first, me["connections"][0]["connection_id"])
    assert status == 200 and sent["priority"] == "standard"


@pytest.mark.parametrize("state", ["incomplete", "unpaid", "paused", "canceled", "checkout_pending"])
def test_nonpaying_billing_states_stay_standard(env, monkeypatch, state):
    _use_test_billing(monkeypatch)
    email = f"{state}@example.test"
    _contractor_billing(env, email, state=state)
    c, _ = signup(env, email, "State Mechanical", code=env["code_a"])
    status, req = _request(c, priority="priority", plan="contractor_pro")
    assert status == 201 and req["priority"] == "standard"


def test_free_contractor_request_is_standard(env, monkeypatch):
    _use_test_billing(monkeypatch)
    email = "free@example.test"
    _contractor_billing(env, email, with_account=False)
    c, _ = signup(env, email, "Free Mechanical", code=env["code_a"])
    status, req = _request(c, priority="priority", plan="contractor_pro", entitlement="contractor_pro")
    assert status == 201 and req["priority"] == "standard"


def test_forged_organization_cannot_borrow_another_pro_entitlement(env, monkeypatch):
    _use_test_billing(monkeypatch)
    pro_org = _contractor_billing(env, "other-pro@example.test")
    email = "plain@example.test"
    _contractor_billing(env, email, with_account=False)
    c, _ = signup(env, email, "Plain Mechanical", code=env["code_a"])
    status, req = _request(c, priority="priority", organization_id=pro_org, plan="contractor_pro",
                           account_type="contractor", entitlement="contractor_pro")
    assert status == 201 and req["priority"] == "standard"


def test_wrong_plan_payment_does_not_grant_priority(env, monkeypatch):
    _use_test_billing(monkeypatch)
    email = "wrong-plan@example.test"
    _contractor_billing(env, email, price=PRICE, state="active")
    c, _ = signup(env, email, "Wrong Plan Mechanical", code=env["code_a"])
    assert _request(c, priority="priority")[1]["priority"] == "standard"


def test_supplier_organization_and_supplier_session_do_not_get_priority(env, monkeypatch):
    _use_test_billing(monkeypatch)
    email = "supplier-match@example.test"
    _contractor_billing(env, email, account_type="supplier", price=PRICE, state="active")
    c, _ = signup(env, email, "Looks Like Supply", code=env["code_a"])
    assert _request(c, priority="priority", account_type="supplier")[1]["priority"] == "standard"
    supplier, _ = login(env, "inbox-a@example.test")
    status, _body = supplier.call("POST", "/api/pilot/contractor/requests", {
        "title": "Nope", "lines": [{"raw_description": "pipe", "quantity": 1}],
        "priority": "priority", "plan": "contractor_pro",
    })
    assert status == 403


# ---------------------------------------------------------------- 7-9 attribution + language

def test_referral_attribution_survives_signup(world):
    conn = db(world["env"])
    prof = conn.execute("SELECT * FROM contractor_profiles WHERE business_name='Beta Mechanical'").fetchone()
    assert prof["referral_code"] == world["env"]["code_a"]
    assert prof["referring_supplier_tenant_id"] == "supplier-a"
    assert prof["acquisition_source"] == "supplier_referral" and prof["acquisition_channel"] == "supplier_referral"
    assert prof["linked_company_ref"] is None  # never linked to the shared company universe
    ev = conn.execute("SELECT * FROM acquisition_events WHERE event='signup_completed' AND contractor_tenant_id=?",
                      (prof["tenant_id"],)).fetchone()
    assert ev["referral_code"] == world["env"]["code_a"] and ev["supplier_tenant_id"] == "supplier-a"
    assert [c["supplier_name"] for c in world["me_b"]["connections"]] == ["Synthetic Supply A"]


def test_utm_attribution_survives_signup_and_later_events(world):
    req = new_request(world["a"])
    send(world["a"], req, world["conn_a"])
    conn = db(world["env"])
    prof = conn.execute("SELECT * FROM contractor_profiles WHERE business_name='Alpha Plumbing'").fetchone()
    assert (prof["utm_source"], prof["utm_medium"], prof["utm_campaign"]) == ("supplier_a", "qr", "counter_pilot")
    assert prof["acquisition_channel"] == "qr"
    for event in ("signup_completed", "material_request_created", "material_request_sent"):
        row = conn.execute("SELECT * FROM acquisition_events WHERE event=? AND contractor_tenant_id=?",
                           (event, prof["tenant_id"])).fetchone()
        assert row is not None, event
        assert (row["channel"], row["utm_medium"], row["utm_campaign"]) == ("qr", "qr", "counter_pilot"), event


def test_channels_are_distinguishable():
    assert analytics.derive_channel({"utm_medium": "qr"}, "c") == "qr"
    assert analytics.derive_channel({"utm_medium": "sales_rep"}, "c") == "sales_rep"
    assert analytics.derive_channel({"utm_source": "linkedin"}, None) == "linkedin"
    assert analytics.derive_channel({}, "code") == "supplier_referral"
    assert analytics.derive_channel({"utm_campaign": "x"}, None) == "campaign"
    assert analytics.derive_channel({}, None) == "direct"
    assert analytics.clean_utm({"utm_source": "<script>", "utm_medium": "QR", "evil": "x"}) == {"utm_medium": "qr"}


def test_login_repeat_login_and_onboarding_events(env):
    c, _ = signup(env, "events@example.test", "Gamma Pipe")
    c.call("GET", "/api/pilot/me")
    c.call("GET", "/api/pilot/me")
    login(env, "events@example.test")
    login(env, "events@example.test")
    conn = db(env)
    counts = dict(conn.execute("SELECT event, COUNT(*) FROM acquisition_events GROUP BY event").fetchall())
    assert counts["contractor_onboarding_completed"] == 1
    assert counts["login"] == 2 and counts["repeat_login"] == 2  # signup already logged in once


def test_client_events_are_allowlisted_and_store_no_personal_data(env):
    c = client(env)
    assert c.call("POST", "/api/pilot/events", {"event": "supplier_referral_view", "referral_code": env["code_a"],
                                                 "visit_id": "v-abc12345", "page": "contractor-join",
                                                 "utm": {"utm_medium": "qr"}})[0] == 202
    assert c.call("POST", "/api/pilot/events", {"event": "signup_completed"})[0] == 400
    assert c.call("POST", "/api/pilot/events", {"event": "login"})[0] == 400
    row = db(env).execute("SELECT * FROM acquisition_events WHERE event='supplier_referral_view'").fetchone()
    assert row["supplier_tenant_id"] == "supplier-a" and row["channel"] == "qr" and row["page"] == "contractor-join"
    cols = {r[1] for r in db(env).execute("PRAGMA table_info(acquisition_events)")}
    assert not cols & {"ip_address", "user_agent", "email", "name", "phone", "token", "password"}


def test_language_preference_persists(env):
    c, me = signup(env, "es@example.test", "Delta Plomería", language="es")
    assert me["language"] == "es"
    _, again = login(env, "es@example.test")
    assert again["language"] == "es"
    assert c.call("PATCH", "/api/pilot/me/language", {"language": "en"})[1] == {"language": "en"}
    _, after = login(env, "es@example.test")
    assert after["language"] == "en"
    assert c.call("PATCH", "/api/pilot/me/language", {"language": "fr"})[0] == 400


# ---------------------------------------------------------------- 10 translations

def _i18n():
    text = (REPO / "pilot-i18n.js").read_text(encoding="utf-8")
    blocks = {}
    for lang in ("en", "es"):
        body = re.search(rf"\n  {lang}: \{{(.*?)\n  \}},", text, re.S).group(1)
        blocks[lang] = dict(re.findall(r'^\s+(\w+): "((?:[^"\\]|\\.)*)",\s*$', body, re.M))
    return blocks


def test_translations_cover_every_pilot_string():
    tr = _i18n()
    assert set(tr["en"]) == set(tr["es"]) and len(tr["en"]) > 80
    used = set()
    for name in ("contractor-join", "contractor-account", "contractor-home", "contractor-request"):
        for ext in (".html", ".js"):
            src = (REPO / (name + ext)).read_text(encoding="utf-8")
            used |= set(re.findall(r'data-i18n(?:-ph)?="(\w+)"', src))
            used |= set(re.findall(r'P\.th?\("(\w+)"', src))
    used |= set(re.findall(r'P\.t\("(\w+)"', (REPO / "pilot-common.js").read_text(encoding="utf-8")))
    missing = {k for k in used if k not in tr["en"] or k not in tr["es"]}
    assert not missing, missing
    for k in used:
        assert tr["es"][k].strip(), k
        # Placeholders must match between languages.
        assert set(re.findall(r"\{\w+\}", tr["en"][k])) == set(re.findall(r"\{\w+\}", tr["es"][k])), k
    # Every server error code a contractor can hit has a translation.
    codes = set(re.findall(r'PilotError\("(\w+)"', (REPO / "pipeline/pilot/accounts.py").read_text(encoding="utf-8")
                           + (REPO / "pipeline/pilot/materials.py").read_text(encoding="utf-8")))
    codes -= {"forbidden", "not_found", "connection_not_found", "invalid_language", "invalid_paste",
              "invalid_reference", "confirmation_required"}
    assert {"err_" + c for c in codes} <= set(tr["es"]), codes


def test_required_pilot_strings_are_present_in_both_languages():
    tr = _i18n()
    for key in ("join_title", "create_account", "sign_in", "sign_out", "new_request", "my_requests", "connections",
                "account", "req_title", "col_description", "col_qty", "add_line", "paste_title", "review",
                "send_to", "confirm_title", "confirm_body", "confirm_send", "sent_title", "share_new", "status_draft"):
        assert tr["en"][key] and tr["es"][key] and tr["en"][key] != tr["es"][key], key
    assert tr["es"]["join_title"] == "Conéctese con {supplier} a través de CorridorIQ"
    assert tr["en"]["join_title"] == "Connect with {supplier} through CorridorIQ"


# ---------------------------------------------------------------- 11 referral token / QR

def test_public_referral_exposes_no_internal_identifier(env):
    code = env["code_a"]
    assert code != "supplier-a" and not code.startswith("supplier-a") and accounts.REFERRAL_CODE_RE.fullmatch(code)
    status, info = client(env).call("GET", f"/api/pilot/referral/{code}")
    assert status == 200 and info == {"code": code, "supplier_name": "Synthetic Supply A"}
    assert client(env).call("GET", "/api/pilot/referral/supplier-a")[0] == 404
    assert client(env).call("GET", "/api/pilot/referral/../../x")[0] in (401, 404)


def test_qr_url_carries_only_public_values():
    url = referral_url("https://corridoriq.pro", "supa-abc234", {"utm_source": "supplier_a", "utm_medium": "qr",
                                                                "token": "secret"})
    assert url == "https://corridoriq.pro/join/supa-abc234?utm_source=supplier_a&utm_medium=qr"
    assert check_public_url(url) == url
    for bad in ("https://user:pw@corridoriq.pro/join/supa-abc234",
                "https://corridoriq.pro/join/supa-abc234?session=abc",
                "https://corridoriq.pro/join/supa-abc234#token",
                "https://corridoriq.pro/contractor-home.html",
                "javascript:alert(1)"):
        with pytest.raises(SystemExit):
            check_public_url(bad)


def _decode_qr(mod):
    """Independent read-back: format bits (second copy), unmask, de-interleave,
    Reed-Solomon syndromes, byte-mode payload."""
    n = len(mod)
    ver = (n - 17) // 4
    fmt = sum(int(mod[8][n - 1 - i]) << i for i in range(8)) | sum(int(mod[n - 15 + i][8]) << i for i in range(8, 15))
    best = None
    for ecl, eb in (("L", 1), ("M", 0)):
        for mask in range(8):
            data = eb << 3 | mask
            rem = data
            for _ in range(10):
                rem = (rem << 1) ^ ((rem >> 9) * 0x537)
            code = (data << 10 | rem) ^ 0x5412
            dist = bin(code ^ fmt).count("1")
            if best is None or dist < best[0]:
                best = (dist, ecl, mask)
    assert best[0] == 0, "format bits do not decode"
    _, ecl, mask = best
    m = qr._Matrix(ver)
    m.function_patterns()
    bits = []
    right = n - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(n):
            for j in range(2):
                x = right - j
                y = n - 1 - vert if ((right + 1) & 2) == 0 else vert
                if not m.fn[y][x]:
                    inv = ((x + y) % 2 == 0, y % 2 == 0, x % 3 == 0, (x + y) % 3 == 0,
                           (x // 3 + y // 2) % 2 == 0, x * y % 2 + x * y % 3 == 0,
                           (x * y % 2 + x * y % 3) % 2 == 0, ((x + y) % 2 + x * y % 3) % 2 == 0)[mask]
                    bits.append(int(mod[y][x]) ^ int(inv))
        right -= 2
    raw = qr._raw_modules(ver) // 8
    words = [int("".join(map(str, bits[i * 8:i * 8 + 8])), 2) for i in range(raw)]
    ecc_len, nb = qr._ECC[ecl][ver]
    short = raw // nb
    num_short = nb - raw % nb
    blocks = [[] for _ in range(nb)]
    k = 0
    for i in range(short + 1):
        for j in range(nb):
            if i == short - ecc_len and j < num_short:
                continue  # short blocks have one data codeword fewer
            blocks[j].append(words[k])
            k += 1
    data = []
    for blk in blocks:
        # syndromes S_i = blk(alpha^i), i = 0..ecc_len-1 must all be zero
        alpha = 1
        for _ in range(ecc_len):
            s = 0
            for w in blk:
                s = qr._gf_mul(s, alpha) ^ w
            assert s == 0, "Reed-Solomon check failed"
            alpha = qr._gf_mul(alpha, 2)
        data += blk[:len(blk) - ecc_len]
    bitstr = "".join(f"{w:08b}" for w in data)
    assert bitstr[:4] == "0100"
    length = int(bitstr[4:12], 2)
    return bytes(int(bitstr[12 + 8 * i:20 + 8 * i], 2) for i in range(length)).decode("utf-8")


@pytest.mark.parametrize("text", ["https://corridoriq.pro/join/supa-abc234",
                                  "https://corridoriq.pro/join/sonoran-k7m2qp?utm_source=sonoran&utm_medium=qr&utm_campaign=counter_pilot",
                                  "x" * 100])
def test_qr_encodes_exactly_the_given_url(text):
    mod = qr.matrix(text)
    assert _decode_qr(mod) == text
    svg = qr.svg(text)
    assert svg.startswith("<svg") and "<script" not in svg and "http" not in svg.split(">", 1)[1]


# ---------------------------------------------------------------- 12 authentication boundaries

PRIVATE = [("GET", "/api/pilot/me"), ("GET", "/api/pilot/contractor/requests"),
           ("GET", "/api/pilot/contractor/requests/r-0000000000000000"), ("GET", "/api/pilot/contractor/connections"),
           ("GET", "/api/pilot/supplier/requests"), ("GET", "/api/pilot/supplier/requests/s-0000000000000000"),
           ("POST", "/api/pilot/contractor/requests"), ("POST", "/api/pilot/contractor/lines/parse"),
           ("POST", "/api/pilot/supplier/requests/s-0000000000000000/acknowledge"),
           ("PATCH", "/api/pilot/me/language"), ("PATCH", "/api/pilot/contractor/profile"),
           ("POST", "/api/pilot/auth/logout")]


@pytest.mark.parametrize("method,path", PRIVATE)
def test_unauthenticated_requests_are_refused(env, method, path):
    status, body = client(env).call(method, path, {} if method != "GET" else None)
    assert status == 401 and body == {"error": "authentication_required"}


def test_session_probe_reveals_nothing_when_signed_out(world):
    assert client(world["env"]).call("GET", "/api/pilot/session") == (200, {"signed_in": False})
    c = client(world["env"])
    c.cookie = f"{PILOT_COOKIE}=forged-token"
    assert c.call("GET", "/api/pilot/session") == (200, {"signed_in": False})
    status, s = world["a"].call("GET", "/api/pilot/session")
    assert status == 200 and s["signed_in"] is True and s["contractor"]["business_name"] == "Alpha Plumbing"


def test_forged_or_revoked_sessions_are_refused(world):
    c = client(world["env"])
    c.cookie = f"{PILOT_COOKIE}=forged-token"
    assert c.call("GET", "/api/pilot/me")[0] == 401
    a = world["a"]
    old = a.cookie
    assert a.call("POST", "/api/pilot/auth/logout", {})[0] == 200
    a.cookie = old
    assert a.call("GET", "/api/pilot/me")[0] == 401


def test_pilot_and_staff_sessions_are_separate(world, monkeypatch):
    # A pilot cookie is not a portal session: staff routes still require their own.
    monkeypatch.setattr(server_mod, "get_current_user", lambda conn, token: None)
    monkeypatch.setattr(server_mod, "_factory", lambda: sqlite3.connect(":memory:"))
    status, _ = world["a"].call("GET", "/api/sales/dashboard")
    assert status == 401
    # A portal cookie is not a pilot session.
    c = client(world["env"])
    c.staff_cookie = "corridoriq_session=" + world["a"].cookie.split("=", 1)[1]
    assert c.call("GET", "/api/pilot/me")[0] == 401


def test_bad_password_and_lockout_use_portal_auth(env):
    signup(env, "lock@example.test", "Lock Co")
    c = client(env)
    for _ in range(5):
        assert c.call("POST", "/api/pilot/auth/login", {"email": "lock@example.test", "password": "wrong-pass"})[1] == {
            "error": "invalid_login"}
    status, body = c.call("POST", "/api/pilot/auth/login", {"email": "lock@example.test", "password": PW})
    assert status == 401 and body == {"error": "invalid_login"}  # locked
    events = {r[0] for r in db(env).execute("SELECT event_type FROM security_audit_log")}
    assert {"login_failure", "account_locked", "pilot_signup"} <= events
    row = db(env).execute("SELECT password_hash FROM users WHERE normalized_email='lock@example.test'").fetchone()
    assert row["password_hash"].startswith(("$argon2", "scrypt$")) and PW not in row["password_hash"]


def test_writes_require_json(env):
    c = client(env)
    status, body = c.call("POST", "/api/pilot/auth/signup", "email=x@example.test&password=12345678",
                          ctype="application/x-www-form-urlencoded")
    assert status == 415 and body["error"] == "json_required"


def test_signup_validation_and_duplicates(env):
    from pipeline.auth import mail
    c = client(env)
    base = {"name": "N", "business_name": "Biz", "email": "dup@example.test"}
    assert c.call("POST", "/api/pilot/auth/signup", dict(base, email="nope"))[1]["error"] == "invalid_email"
    assert c.call("POST", "/api/pilot/auth/signup", dict(base, business_name=" "))[1]["error"] == "business_required"
    assert c.call("POST", "/api/pilot/auth/signup", dict(base, name=""))[1]["error"] == "name_required"
    status, resp = c.call("POST", "/api/pilot/auth/signup", base)
    assert status == 202 and resp == {"status": "check_email"}
    assert c.cookie is None
    assert client(env).call("POST", "/api/pilot/auth/signup", base) == (202, {"status": "check_email"})
    assert client(env).call("GET", "/api/pilot/me")[0] == 401
    raw = _sink_token("dup@example.test", mail.PILOT_SIGNUP)
    assert c.call("POST", "/api/pilot/auth/complete-signup", {"token": raw, "password": "short"})[1]["error"] == "weak_password"
    status, me = c.call("POST", "/api/pilot/auth/complete-signup", {"token": raw, "password": PW})
    assert status == 201 and me["contractor"]["email_verification"] == "verified"
    assert client(env).call("POST", "/api/pilot/auth/signup", base) == (202, {"status": "check_email"})
    assert len(mail.sink().outbox(to="dup@example.test", kind=mail.PILOT_SIGNUP)) == 2


def test_pilot_disabled_without_root(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_ROOT, raising=False)
    monkeypatch.setattr(server_mod, "_factory", lambda: (_ for _ in ()).throw(AssertionError("opened")))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.ApiHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = Client(srv.server_address[1])
        assert c.call("GET", "/api/pilot/me") == (503, {"error": "pilot_not_enabled"})
        assert c.call("POST", "/api/pilot/auth/signup", {})[0] == 503
    finally:
        srv.shutdown()
        srv.server_close()


def test_pilot_root_never_holds_the_production_database(tmp_path, monkeypatch):
    from pipeline.config import settings
    prod = tmp_path / "db" / "corridoriq.db"
    monkeypatch.setattr(settings, "DB_PATH", prod)
    for bad in (tmp_path, tmp_path / "db", prod, None, ""):
        with pytest.raises(PilotNotConfigured):
            validate_root(bad)
    validate_root(tmp_path / "pilot")
    intel = tmp_path / "pilot2"
    intel.mkdir()
    c = sqlite3.connect(intel / "platform.db")
    c.execute("CREATE TABLE permits (id INTEGER)")
    c.commit()
    c.close()
    with pytest.raises(PilotNotConfigured):
        Platform(intel).connect()


# ---------------------------------------------------------------- 15 no catalog / pricing / stock

_CATALOG_KEYS = re.compile(r'"(?:price|unit_price|cost|cost_price|sku|quantity_available|qty_available|inventory|'
                           r'stock|in_stock|list_price|account_price|availability)"', re.I)


def test_contractor_responses_contain_no_catalog_pricing_or_stock(world):
    a = world["a"]
    req = new_request(a)
    bodies = [a.call("GET", "/api/pilot/me")[1], a.call("GET", "/api/pilot/contractor/requests")[1],
              a.call("GET", f"/api/pilot/contractor/requests/{req['request_id']}")[1],
              a.call("GET", "/api/pilot/contractor/connections")[1],
              a.call("POST", "/api/pilot/contractor/lines/parse", {"text": "12 ea 1/2in elbow\nvalve x 3"})[1],
              send(a, req, world["conn_a"])[1],
              client(world["env"]).call("GET", f"/api/pilot/referral/{world['env']['code_a']}")[1]]
    for b in bodies:
        assert not _CATALOG_KEYS.search(json.dumps(b)), b


def test_pilot_code_never_reads_catalog_or_intelligence_modules():
    for py in (REPO / "pipeline" / "pilot").glob("*.py"):
        src = py.read_text(encoding="utf-8")
        for mod in ("pipeline.products", "pipeline.suppliers", "pipeline.crm", "pipeline.db.database",
                    "pipeline.company_resolution", "pipeline.entity", "pipeline.roc", "pipeline.identity_shadow",
                    "pipeline.book_match", "pipeline.ingestion", "pipeline.relevance", "pipeline.sales_lanes"):
            assert mod not in src, (py.name, mod)


PILOT_PAGES = ["contractor-join", "contractor-account", "contractor-home", "contractor-request", "supplier-inbox"]


@pytest.mark.parametrize("name", PILOT_PAGES)
def test_pilot_pages_make_no_stock_pricing_or_partnership_claims(name):
    text = ""
    for ext in (".html", ".js"):
        text += (REPO / (name + ext)).read_text(encoding="utf-8")
    text += (REPO / "pilot-i18n.js").read_text(encoding="utf-8")
    for pat in (r"inventory", r"in[- ]stock", r"\$\s?\d", r"live (?:pric|availab)", r"account pric",
                r"partner(?:ed|ship)? with", r"endorse", r"official (?:partner|supplier)", r"guarantee",
                r"sonoran"):
        assert not re.search(pat, text, re.I), (name, pat)


# ---------------------------------------------------------------- 16 fail-closed tenancy

def test_malformed_and_foreign_ids_fail_closed(world):
    a = world["a"]
    for path in ("/api/pilot/contractor/requests/r-zz", "/api/pilot/contractor/requests/../../platform",
                 "/api/pilot/contractor/requests/r-00000000000000000000", "/api/pilot/supplier/requests/x"):
        assert a.call("GET", path)[0] in (403, 404), path
    assert a.call("GET", "/api/pilot/contractor/requests/r-0123456789abcdef")[0] == 404


def test_contractor_context_cannot_be_forged_or_crossed(world):
    platform = world["env"]["platform"]
    with pytest.raises(TenantError):
        ContractorContext(ContractorTenant("c-aaaaaaaaaaaa", "X", True, "2026-01-01"))
    sup_ctx = platform.suppliers.context("supplier-a")
    with pytest.raises(TenantError):
        ContractorStore(sup_ctx, platform.contractors)
    with pytest.raises(TenantError):
        platform.suppliers.store(platform.contractors.context(
            db(world["env"]).execute("SELECT tenant_id FROM contractor_profiles LIMIT 1").fetchone()[0]))


def test_swapped_contractor_store_is_refused(world):
    platform = world["env"]["platform"]
    tids = [r[0] for r in db(world["env"]).execute("SELECT tenant_id FROM contractor_profiles ORDER BY created_at")]
    new_request(world["a"])
    new_request(world["b"])
    da, dbb = (platform.contractors.tenant_dir(t) / "contractor.db" for t in tids[:2])
    tmp = da.with_suffix(".tmp")
    da.rename(tmp)
    dbb.rename(da)
    status, _ = world["a"].call("GET", "/api/pilot/contractor/requests")
    assert status == 403  # owner stamp mismatch fails closed (no data returned)


def test_inactive_contractor_tenant_loses_access(world):
    platform = world["env"]["platform"]
    tid = db(world["env"]).execute("SELECT tenant_id FROM contractor_profiles WHERE business_name='Alpha Plumbing'"
                                   ).fetchone()[0]
    platform.contractors.set_active(tid, False)
    assert world["a"].call("GET", "/api/pilot/me")[0] == 401
    assert client(world["env"]).call("POST", "/api/pilot/auth/login",
                                     {"email": "owner-a@example.test", "password": PW})[0] == 401


def test_supplier_registry_still_refuses_contractor_tenants(tmp_path):
    from pipeline.tenancy import TenantRegistry, TenantType
    reg = TenantRegistry(tmp_path / "r")
    with pytest.raises(ValueError):
        reg.create("contractor-a", "C", tenant_type="contractor")
    assert [t.value for t in TenantType] == ["supplier"]


# ---------------------------------------------------------------- material lines

def test_raw_text_is_preserved_exactly(world):
    desc = "  1/2\" Uponor ProPEX ring — w/ stop  "
    req = new_request(world["a"], lines=[{"raw_description": desc, "quantity": "3.5", "uom": "bx", "note": "nota ñ"}])
    assert req["lines"][0]["raw_description"] == desc
    assert req["lines"][0]["quantity"] == 3.5 and req["lines"][0]["note"] == "nota ñ"
    for bad in ([{"raw_description": " ", "quantity": 1}], [{"raw_description": "x", "quantity": 0}],
                [{"raw_description": "x", "quantity": "abc"}], [], [{"raw_description": "x" * 501, "quantity": 1}]):
        status, err = world["a"].call("POST", "/api/pilot/contractor/requests", {"title": "t", "lines": bad})
        assert status == 400, bad


def test_paste_parsing_is_conservative():
    rows = materials.parse_pasted("12 ea 1/2in copper elbow\n1/2in ball valve x 6\n2 1/2in PVC coupling\n"
                                  "3/4 PEX 100ft\nPVC cement, 4, can\n\n10 Sharkbite 3/4 tee\n")
    got = [(r["raw_description"], r["quantity"], r["uom"]) for r in rows]
    assert got == [("1/2in copper elbow", 12.0, "ea"), ("1/2in ball valve", 6.0, None),
                   ("2 1/2in PVC coupling", 1.0, None), ("3/4 PEX 100ft", 1.0, None),
                   ("PVC cement", 4.0, "can"), ("Sharkbite 3/4 tee", 10.0, None)]
    assert [r["source_text"] for r in rows][2] == "2 1/2in PVC coupling"


def test_store_tables_carry_tenant_and_no_supplier_fields(world):
    req = new_request(world["a"])
    platform = world["env"]["platform"]
    tid = db(world["env"]).execute("SELECT tenant_id FROM contractor_profiles WHERE business_name='Alpha Plumbing'"
                                   ).fetchone()[0]
    path = platform.contractors.tenant_dir(tid) / "contractor.db"
    c = sqlite3.connect(path)
    assert c.execute("SELECT tenant_id, tenant_type FROM tenant_meta").fetchone() == (tid, "contractor")
    assert {r[0] for r in c.execute("SELECT DISTINCT tenant_id FROM material_request_lines")} == {tid}
    assert c.execute("SELECT request_id FROM material_requests").fetchone()[0] == req["request_id"]


def test_priority_sorts_ahead_of_an_earlier_standard_request(env, monkeypatch):
    _use_test_billing(monkeypatch)
    standard, me_s = signup(env, "std-order@example.test", "Standard Order Co", code=env["code_a"])
    pro_email = "pro-order@example.test"
    _contractor_billing(env, pro_email)
    priority, me_p = signup(env, pro_email, "Priority Order Co", code=env["code_a"])
    first = new_request(standard, "Earlier standard")
    assert send(standard, first, me_s["connections"][0]["connection_id"])[0] == 200
    ahead = new_request(priority, "Later priority")
    assert ahead["priority"] == "priority"
    assert send(priority, ahead, me_p["connections"][0]["connection_id"])[0] == 200
    later = new_request(standard, "Later standard")
    assert send(standard, later, me_s["connections"][0]["connection_id"])[0] == 200
    # Force a send-time conflict: the priority request is newer than both standard requests.
    stored = db(env)
    stored.execute("UPDATE request_shares SET sent_at='2026-10-05T12:00:02+00:00' WHERE title='Later priority'")
    stored.execute("UPDATE request_shares SET sent_at='2026-10-05T12:00:01+00:00' WHERE title='Earlier standard'")
    stored.execute("UPDATE request_shares SET sent_at='2026-10-05T12:00:03+00:00' WHERE title='Later standard'")
    stored.commit()
    stored.close()
    supplier, _ = login(env, "inbox-a@example.test")
    items = supplier.call("GET", "/api/pilot/supplier/requests")[1]["items"]
    assert [item["title"] for item in items] == ["Later priority", "Earlier standard", "Later standard"]
    assert [item["priority"] for item in items] == ["priority", "standard", "standard"]
    other = login(env, "inbox-b@example.test")[0]
    assert other.call("GET", "/api/pilot/supplier/requests")[1]["items"] == []


def test_supplier_inbox_list_labels_priority_and_standard():
    text = (REPO / "supplier-inbox.js").read_text(encoding="utf-8")
    listing = text.split("function renderDetail")[0]
    assert 'chip new">Priority' in listing
    assert 'chip">Standard' in listing


def test_trusted_proxy_uses_cloudflare_ip_and_a_direct_request_cannot_spoof(env, monkeypatch):
    from pipeline.api.client_address import resolve_client_ip

    secret = "unit-proxy-secret"
    monkeypatch.setenv("CORRIDORIQ_TRUSTED_PROXY_SECRET", secret)
    trusted = {"CF-Connecting-IP": "203.0.113.10", "X-CorridorIQ-Proxy-Secret": secret}
    assert resolve_client_ip("127.0.0.1", trusted) == "203.0.113.10"
    forged = {"CF-Connecting-IP": "203.0.113.10", "X-Forwarded-For": "198.51.100.4"}
    assert resolve_client_ip("127.0.0.1", forged) == "127.0.0.1"
    assert resolve_client_ip("198.51.100.8", trusted) == "198.51.100.8"
    assert resolve_client_ip("127.0.0.1", {"CF-Connecting-IP": "not an ip", "X-CorridorIQ-Proxy-Secret": secret}) == "127.0.0.1"
    from pipeline.auth import throttle
    throttle.pilot.clear()
    pilot_api._hits.clear()
    c = client(env)
    for _ in range(10):
        status, _ = c.call("POST", "/api/pilot/auth/signup", {"email": "x"}, headers=trusted)
        assert status != 429
    assert c.call("POST", "/api/pilot/auth/signup", {"email": "x"}, headers=trusted)[0] == 429
    other = {"CF-Connecting-IP": "203.0.113.11", "X-CorridorIQ-Proxy-Secret": secret}
    assert c.call("POST", "/api/pilot/auth/signup", {"email": "x"}, headers=other)[0] != 429
    # A forged address without the secret shares the loopback bucket.
    spoofed = {"CF-Connecting-IP": "203.0.113.99"}
    for _ in range(10):
        assert c.call("POST", "/api/pilot/auth/login", {"email": "nobody@example.test", "password": "wrong-pass"},
                      headers=spoofed)[0] != 429
    moved = {"CF-Connecting-IP": "203.0.113.100"}
    assert c.call("POST", "/api/pilot/auth/login", {"email": "nobody@example.test", "password": "wrong-pass"},
                  headers=moved)[0] != 429
    for _ in range(20):
        c.call("POST", "/api/pilot/auth/login", {"email": "nobody@example.test", "password": "wrong-pass"},
               headers=spoofed)
    assert c.call("POST", "/api/pilot/auth/login", {"email": "nobody@example.test", "password": "wrong-pass"},
                  headers=moved)[0] == 429


def test_production_pilot_cookie_is_secure(env, monkeypatch):
    monkeypatch.setattr("pipeline.config.settings.AUTH_PRODUCTION", True)
    c, _ = signup(env, "secure-cookie@example.test", "Secure Cookie Co", code=env["code_a"])
    assert "Secure" in c.last_pilot_cookie and "HttpOnly" in c.last_pilot_cookie


def test_join_route_serves_the_join_page_only_for_valid_codes(env):
    status, body = client(env).call("GET", "/join/supa-abc234")
    assert status == 200 and "contractor-join.js" in body
    for bad in ("/join/../platform.db", "/join/ab", "/join/UPPER-case", "/join/a/b"):
        assert client(env).call("GET", bad)[0] == 404, bad
