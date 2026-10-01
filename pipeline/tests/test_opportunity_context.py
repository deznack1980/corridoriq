"""Dashboard Top Opportunities -> View All -> Opportunities use one context."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pipeline.auth import service as auth
from pipeline.auth.rbac import AuthzError
from pipeline.crm import service as crm
from pipeline.crm.service import ValidationError
from pipeline.sales_lanes.serialize import BANNED_PUBLIC_FIELDS
from pipeline.trust.contacts import verified_contact_view
from pipeline.tests.test_trust_contacts import _chan
from pipeline.tests.test_trust_portal import _keys, world  # noqa: F401  (fixture)
from pipeline.tests.test_workspace_ui import _ctx, conn, env  # noqa: F401  (fixtures)

REPO = Path(__file__).resolve().parents[2]


def _ids(res):
    return {i["company_id"] for i in res["items"]}


def test_dashboards_request_and_link_the_same_context():
    for page, ctx in (("admin-dashboard.js", "organization"), ("sales-dashboard.js", "assigned")):
        js = (REPO / page).read_text(encoding="utf-8")
        assert f'const OPP_CONTEXT = "{ctx}"' in js
        assert "CIQ.mapWorkspace(" in js and "context: OPP_CONTEXT" in js
        assert 'const OPP_HREF = "opportunities.html?context=" + OPP_CONTEXT' in js
        assert "listHref: OPP_HREF" in js
        # No silent fallback to a different feed.
        feed = js[js.index("function renderFeed"):]
        feed = feed[: feed.index("\n  }\n")]
        assert "recent_opportunity_activity" not in feed
    # The workspace builds the feed, the map, and View all from one params().
    mw = (REPO / "map-workspace.js").read_text(encoding="utf-8")
    assert '"/api/sales/opportunities?" + params({ page_size: FEED_SIZE })' in mw
    assert '"/api/sales/opportunities/map?" + params()' in mw
    assert '"opportunities.html?" + params()' in mw
    page_js = (REPO / "opportunities.js").read_text(encoding="utf-8")
    assert 'incoming.get("context")' in page_js and 'incoming.get("scope")' in page_js


def test_top_opportunities_is_page_one_of_view_all(world):
    c = world["conn"]
    for user, ctx in ((world["admin"], "organization"), (world["mgr"], "assigned")):
        top = crm.opportunities(c, user, {"context": ctx, "page_size": 6})
        full = crm.opportunities(c, user, {"context": ctx, "page_size": 50})
        assert top["context"] == full["context"] == ctx
        assert [i["project_id"] for i in top["items"]] == [i["project_id"] for i in full["items"][:6]]
        assert top["total"] == full["total"]


def test_qualifying_project_in_both_contexts_and_off_focus_never(world):
    c = world["conn"]
    for ctx in ("assigned", "organization"):
        res = crm.opportunities(c, world["admin"], {"context": ctx})
        assert world["co1"] in _ids(res), ctx
        assert world["dealer"] not in _ids(res), ctx
        everything = crm.opportunities(c, world["admin"], {"context": ctx, "scope": "all"})
        assert world["dealer"] in _ids(everything), ctx  # scope=all still available
        assert everything["include_all_scopes"] is True


def test_permissions(world):
    c = world["conn"]
    with pytest.raises(AuthzError):
        crm.opportunities(c, world["rep"], {"context": "organization"})
    rep = crm.opportunities(c, world["rep"], {"context": "assigned"})
    assert _ids(rep) <= {world["co1"], world["dealer"]}  # only assigned companies
    assert world["co2"] not in _ids(crm.opportunities(c, world["rep"], {"context": "assigned", "scope": "all"}))
    assert crm.opportunities(c, world["mgr"], {"context": "organization"})["context"] == "organization"
    with pytest.raises(ValidationError):
        crm.opportunities(c, world["admin"], {"context": "everything"})


def test_empty_state_reasons(env, world):
    c = world["conn"]
    org = world["org"]
    uid = auth.create_user(c, organization_id=org, email="rep3@corridoriq.com", password="Rep3Pass123",
                           role_names=["sales_representative"], must_change_password=False)
    lonely = crm.opportunities(c, _ctx(c, uid), {"context": "assigned"})
    assert lonely["total"] == 0 and lonely["empty_reason"] == "NO_ASSIGNED_COMPANIES"
    filtered = crm.opportunities(c, world["admin"], {"context": "organization", "q": "zzz-no-match"})
    assert filtered["empty_reason"] == "FILTERED"
    # Only off-focus/non-wet projects in scope: the reason says checks hid them.
    c.execute("DELETE FROM crm_company_relationships WHERE company_id<>?", (world["dealer"],))
    c.commit()
    hidden = crm.opportunities(c, world["admin"], {"context": "assigned"})
    assert hidden["total"] == 0 and hidden["empty_reason"] == "NONE_PASS_CHECKS"
    assert "2 projects" in hidden["empty_message"] and "Include all scopes" in hidden["empty_message"]
    assert hidden["hidden_by_checks"] == 2
    ok = crm.opportunities(c, world["admin"], {"context": "organization"})
    assert ok["empty_reason"] is None and ok["empty_message"] is None


def test_no_relationships_and_no_projects(env):
    c = env["conn"]
    c.execute("DELETE FROM crm_company_relationships")
    c.commit()
    res = crm.opportunities(c, env["admin"], {"context": "assigned"})
    assert res["empty_reason"] == "NO_RELATIONSHIPS" and "organization view" in res["empty_message"]
    org = crm.opportunities(c, env["admin"], {"context": "organization"})
    assert org["empty_reason"] == "NO_PROJECTS"


def test_row_contact_equals_company_page_contact(world):
    c = world["conn"]
    _chan(c, world["co1"], "business_phone", "602-555-0120", name="Pat Buyer", title="Purchasing")
    res = crm.opportunities(c, world["mgr"], {"context": "assigned"})
    row = next(i for i in res["items"] if i["company_id"] == world["co1"])
    page = verified_contact_view(c, world["co1"])
    assert row["contact"]["status"] == page["status"] == "VERIFIED"
    assert row["contact"]["phone"] == page["phone"]["value"] == "602-555-0120"
    assert row["contact"]["name"] == page["contact_name"]
    detail = crm.get_company_detail(c, world["mgr"], world["co1"])
    assert detail["verified_contact"] == page


def test_unverified_row_contact_is_not_presented_as_verified(world):
    c = world["conn"]
    _chan(c, world["co1"], "business_phone", "602-555-0121", status="CANDIDATE", source="roc")
    row = next(i for i in crm.opportunities(c, world["mgr"])["items"] if i["company_id"] == world["co1"])
    assert row["contact"]["status"] == "NOT_VERIFIED" and row["contact"]["phone"] is None
    assert "602-555-0121" not in json.dumps(row)


def test_no_internal_fields_in_opportunity_payloads(world):
    c = world["conn"]
    for ctx in ("assigned", "organization"):
        for scope in ("", "all"):
            res = crm.opportunities(c, world["admin"], {"context": ctx, "scope": scope})
            keys = set(_keys(res))
            for banned in (*BANNED_PUBLIC_FIELDS, "trade_identity", "gate_result", "normalized_value",
                           "verification_status", "permit_type", "project_description"):
                assert banned not in keys, (ctx, scope, banned)
            assert not re.search(r'"roc"|VERIFIED_MATCH|account_priority', json.dumps(res))
