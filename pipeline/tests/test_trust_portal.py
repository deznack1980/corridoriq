"""Portal surfaces read the shared trust layer, not the legacy company tier.

Generic by design: the non-relevant account here is a made-up equipment dealer.
Nothing in the trust layer or the CRM service matches on a company name.
"""

from __future__ import annotations

import itertools
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pipeline.crm import admin as crm_admin
from pipeline.crm import service as crm
from pipeline.sales_lanes.serialize import BANNED_PUBLIC_FIELDS
from pipeline.trust import account_view as trust
from pipeline.trust import scope as S
from pipeline.trust.evidence import LANE_VERSION, PROFILE_KEY
from pipeline.tests.test_workspace_ui import _company, _ctx, _intel, conn, env  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parents[2]


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lane(c, company_id, lane, fit="HIGH", presentable=1):
    c.execute(
        "INSERT INTO company_sales_lanes (company_id, lane_key, fit, presentable, model_version, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (company_id, lane, fit, presentable, LANE_VERSION, _now()),
    )
    c.commit()


def _permit_project(c, company_id, description, *, score=90, lifecycle="Permit Issued", days_ago=3, ptype="Building"):
    d = (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    now = _now()
    cur = c.execute(
        "INSERT INTO permits (jurisdiction, permit_number, permit_type, description, job_address, city, state, "
        "status, contractor_company_id, issued_date, first_seen_at, last_updated_at) "
        "VALUES ('phoenix_az',?,?,?,'1 Main St','PHOENIX','AZ','issued',?,?,?,?)",
        (f"T{company_id}-{description[:6]}-{days_ago}", ptype, description, company_id, d, now, now),
    )
    c.execute(
        "INSERT INTO projects (permit_id, jurisdiction, contractor_company_id, project_category, "
        "project_lifecycle, opportunity_score, opportunity_date, opportunity_timing) "
        "VALUES (?, 'phoenix_az', ?, 'commercial', ?, ?, ?, 'immediate')",
        (cur.lastrowid, company_id, lifecycle, score, d),
    )
    c.commit()


@pytest.fixture()
def world(env):
    """A plumber with a low legacy score and an equipment dealer with a top one."""
    from pipeline.sales_lanes.store import seed_lanes

    trust.clear_cache()
    c = env["conn"]
    seed_lanes(c)
    dealer = _company(c, "Desert Forklift Sales")
    _intel(c, dealer, "Critical", 99)
    _lane(c, dealer, "SPECIALTY_OTHER")
    _lane(c, env["co1"], "PLUMBING_CORE")
    c.execute("UPDATE company_intelligence SET company_priority_tier='Low', company_priority_score=20 WHERE company_id=?",
              (env["co1"],))
    c.commit()
    crm.assign_company(c, env["admin"], dealer, env["ids"]["rep"])
    _permit_project(c, dealer, "Install 16 feet tall storage racking system", score=99)
    # A cross-reference to sprinkler plans reads as fire-line scope; the account
    # gate, not a name rule, keeps it out of company-bound feeds.
    _permit_project(c, dealer, "Install steel framed mezzanine. Fire sprinklers plans required - ref: PMT24-02804",
                    score=98, days_ago=4)
    _permit_project(c, env["co1"], "Replace 50 gal water heater", score=61)
    _permit_project(c, env["co1"], "", score=70, days_ago=5)  # blank scope stays, labelled
    env["dealer"] = dealer
    return env


def test_lane_status_rules_are_generic():
    rows = lambda *pairs: [{"lane_key": k, "fit": f, "presentable": 0 if k == "IDENTITY_REVIEW" else 1} for k, f in pairs]
    assert trust.status_from_lanes([]) == trust.NOT_ASSESSED
    assert trust.status_from_lanes(rows(("SPECIALTY_OTHER", "HIGH"))) == trust.NOT_RELEVANT
    assert trust.status_from_lanes(rows(("IDENTITY_REVIEW", "HIGH"))) == trust.NOT_SALES_READY
    assert trust.status_from_lanes(rows(("PLUMBING_CORE", "HIGH"), ("MULTI_TRADE", "HIGH"))) == trust.CORE
    assert trust.status_from_lanes(rows(("FIRE_BACKFLOW", "MEDIUM"))) == trust.ADJACENT
    assert trust.status_from_lanes(rows(("PLUMBING_CORE", "LOW"))) == trust.NOT_RELEVANT


def test_sql_rank_matches_python_status():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE companies (id INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE company_sales_lanes (company_id, lane_key, fit, presentable, model_version, created_at)")
    c.execute("CREATE TABLE company_customer_priority (company_id, profile_key, account_priority_score)")
    lanes = ["PLUMBING_CORE", "FIRE_BACKFLOW", "SPECIALTY_OTHER", "IDENTITY_REVIEW", "GENERAL_CONTRACTOR_CM"]
    combos, cid = [], 0
    for n in range(0, 3):
        for picked in itertools.combinations(lanes, n):
            for fit in ("HIGH", "LOW"):
                cid += 1
                c.execute("INSERT INTO companies VALUES (?)", (cid,))
                rows = [{"lane_key": k, "fit": fit, "presentable": 0 if k == "IDENTITY_REVIEW" else 1} for k in picked]
                for r in rows:
                    c.execute("INSERT INTO company_sales_lanes VALUES (?,?,?,?,?,?)",
                              (cid, r["lane_key"], r["fit"], r["presentable"], LANE_VERSION, "x"))
                combos.append((cid, trust.status_from_lanes(rows)))
    join, rank, params = trust.relevance_sql("c.id")
    got = {r["id"]: r["rk"] for r in c.execute(f"SELECT c.id, {rank} AS rk FROM companies c {join}", params)}
    for cid, status in combos:
        assert got[cid] == trust.RANK[status], (cid, status)


def test_project_scope_has_three_states():
    racking = S.project_trade_scope(permit_type="COM", description="Install 16 feet tall storage racking system")
    blank = S.project_trade_scope(permit_type=None, description="")
    heater = S.project_trade_scope(permit_type="Building", description="Replace 50 gal water heater")
    assert racking["state"] == S.NOT_WET_STATED
    assert blank["state"] == S.SCOPE_NOT_STATED
    assert heater["state"] == S.WET_STATED
    # General building work may include plumbing; it is never hidden.
    for text in ("NEW 3-STORY OFFICE BUILDING", "Tenant improvement for retail suite", "Kitchen remodel"):
        assert S.project_trade_scope(permit_type="COM", description=text)["state"] == S.SCOPE_NOT_STATED, text
    # Hiding needs a named non-wet trade.
    assert S.project_trade_scope(permit_type="COM", description="HIGH PILE COMBUSTIBLE STORAGE")["state"] == S.NOT_WET_STATED
    assert S.project_trade_scope(permit_type="COM", description="Community meeting notes")["state"] == S.SCOPE_NOT_STATED


def test_company_list_orders_by_relevance_not_legacy_score(world):
    items = crm.list_my_companies(world["conn"], world["mgr"])["items"]
    ids = [i["company_id"] for i in items]
    assert ids.index(world["co1"]) < ids.index(world["dealer"])  # legacy 20 beats legacy 99
    dealer = next(i for i in items if i["company_id"] == world["dealer"])
    assert dealer["account_relevance"]["status"] == trust.NOT_RELEVANT
    assert dealer["recommended_action"] == "Review fit before more outreach"
    assert "Outside plumbing-supply focus" in dealer["reason"]


def test_dashboard_never_promotes_non_relevant_accounts(world):
    d = crm.dashboard(world["conn"], world["rep"])
    promoted = [i["company_id"] for i in d["priority_companies"]]
    assert world["co1"] in promoted and world["dealer"] not in promoted
    assert d["kpis"]["high_priority_opportunities"] == 1
    assert "todays_accounts" in d and "data_status" in d["todays_accounts"]
    feed = d["recent_opportunity_activity"]
    assert all(i["company_id"] != world["dealer"] for i in feed)
    assert {i["scope_state"] for i in feed} <= {S.WET_STATED, S.SCOPE_NOT_STATED}


def test_opportunities_hide_stated_non_wet_work_unless_asked(world):
    c = world["conn"]
    default = crm.opportunities(c, world["mgr"])
    assert all(i["company_id"] != world["dealer"] for i in default["items"])
    assert default["total"] == 2  # water heater + blank-scope project
    assert default["items"][0]["opportunity_score"] >= default["items"][-1]["opportunity_score"]
    everything = crm.opportunities(c, world["mgr"], {"scope": "all"})
    dealer_rows = [i for i in everything["items"] if i["company_id"] == world["dealer"]]
    assert len(dealer_rows) == 2
    assert {i["trade_scope"] for i in dealer_rows} == {"No plumbing or wet-side scope stated", "fire line / hydrant / sprinkler"}
    assert all(i["account_relevance"]["status"] == trust.NOT_RELEVANT for i in dealer_rows)


def test_estimator_and_admin_feeds_use_trade_scope(world):
    c = world["conn"]
    q = crm_admin.estimator_work_queue(c, world["admin"])
    assert all(i["company_id"] != world["dealer"] for i in q["queue"])
    dash = crm_admin.admin_dashboard(c, world["admin"])
    assert all(i.get("company_id") != world["dealer"] for i in dash["recent_opportunity_activity"])
    assert "relevant_accounts" in dash["kpis"] and "todays_accounts" in dash


def _keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _keys(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _keys(v)


def test_portal_payloads_expose_no_internal_fields(world):
    c = world["conn"]
    payloads = [
        crm.dashboard(c, world["rep"]),
        crm.list_my_companies(c, world["mgr"]),
        crm.opportunities(c, world["mgr"], {"scope": "all"}),
        crm_admin.admin_dashboard(c, world["admin"]),
        crm_admin.estimator_work_queue(c, world["admin"]),
    ]
    for payload in payloads:
        keys = set(_keys(payload))
        for banned in (*BANNED_PUBLIC_FIELDS, "trade_identity", "gate_result", "evidence_refs"):
            assert banned not in keys, banned


def test_todays_accounts_are_public_and_scoped(tmp_path):
    from agents.ceo.evals import morning_fixture as F

    trust.clear_cache()
    db = F.build(tmp_path / "m.db")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    everyone = trust.todays_accounts(c, as_of=F.AS_OF)
    ids = {i["company_id"] for i in everyone["items"]}
    assert ids == {F.KERNS, F.CLEAN}
    only = trust.todays_accounts(c, as_of=F.AS_OF, company_ids={F.CLEAN})
    assert [i["company_id"] for i in only["items"]] == [F.CLEAN]
    dropped = trust.todays_accounts(c, as_of=F.AS_OF, exclude_ids={F.CLEAN})
    assert F.CLEAN not in {i["company_id"] for i in dropped["items"]}
    blob = json.dumps(everyone)
    for banned in BANNED_PUBLIC_FIELDS:
        assert f'"{banned}"' not in blob
    assert "VERIFIED_MATCH" not in blob and "ROC" not in blob
    # The verified phone is shown on purpose, and only inside contact_details.
    for item in everyone["items"]:
        outside = json.dumps({k: v for k, v in item.items() if k != "contact_details"})
        assert "602-555-0100" not in outside
        assert item["contact_details"]["phone"]["value"] == "602-555-0100"
    card = everyone["items"][0]
    assert card["action"] in {"Call", "Email", "Follow up"}
    assert card["not_observed"] and card["verify_before_contact"]
    c.close()


def test_no_company_name_rules_in_trust_or_crm():
    for path in list((REPO / "pipeline" / "trust").glob("*.py")) + [REPO / "pipeline" / "crm" / "service.py",
                                                                  REPO / "pipeline" / "crm" / "admin.py"]:
        assert not re.search(r"toyota|forklift", path.read_text(encoding="utf-8"), re.I), path.name


def test_trust_layer_has_no_write_sql():
    forbidden = re.compile(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|REPLACE INTO|CREATE)\b|\.commit\(")
    for path in (REPO / "pipeline" / "trust").glob("*.py"):
        assert not forbidden.search(path.read_text(encoding="utf-8")), path.name
