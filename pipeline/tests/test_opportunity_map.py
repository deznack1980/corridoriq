"""Opportunity map: source-published locations only, same set as the feed."""

from __future__ import annotations

import json
import re

import pytest

from pipeline.auth.rbac import AuthzError
from pipeline.crm import service as crm
from pipeline.sales_lanes.serialize import BANNED_PUBLIC_FIELDS
from pipeline.trust import geo
from pipeline.trust.contacts import verified_contact_view
from pipeline.tests.test_trust_contacts import _chan
from pipeline.tests.test_trust_portal import _keys, world  # noqa: F401  (fixture)
from pipeline.tests.test_workspace_ui import conn, env  # noqa: F401  (fixtures)

PEORIA_BOX = (33.55, -112.50, 33.95, -112.15)


# ---------------------------------------------------------------- geo

def test_state_plane_origin_and_roundtrip():
    lat, lon = geo.az_central_ft_to_wgs84(700000.0, 0.0)
    assert abs(lat - 31.0) < 1e-6 and abs(lon - (-111.9166667)) < 1e-6
    for pt in ((33.4484, -112.0740), (33.5806, -112.2374), (33.3062, -111.8413)):
        back = geo.az_central_ft_to_wgs84(*geo.wgs84_to_az_central_ft(*pt))
        assert abs(back[0] - pt[0]) < 1e-7 and abs(back[1] - pt[1]) < 1e-7


def test_peoria_raw_coordinates_are_placed_in_peoria():
    raw = json.dumps({"X_COORD": 595897.79542, "Y_COORD": 963203.8308, "SITUS_ZIP": "85382"})
    loc = geo.permit_location({"jurisdiction": "peoria_az", "raw_source_json": raw})
    s, w, n, e = PEORIA_BOX
    assert loc["precision"] == geo.PRECISION_STATE_PLANE
    assert s <= loc["lat"] <= n and w <= loc["lon"] <= e


def test_no_guessing():
    # Address only (Phoenix today): not placed, never a city centroid.
    assert geo.permit_location({"jurisdiction": "phoenix_az", "job_address": "1215 W ASTER DR"}) is None
    # Zero / out-of-metro coordinates are bad data, not a location.
    assert geo.permit_location({"latitude": 0.0, "longitude": 0.0, "jurisdiction": "gilbert_az"}) is None
    assert geo.permit_location({"latitude": 40.7, "longitude": -74.0, "jurisdiction": "mesa_az"}) is None
    # Peoria with garbage raw coordinates.
    assert geo.permit_location({"jurisdiction": "peoria_az", "raw_source_json": '{"X_COORD": 0}'}) is None
    ok = geo.permit_location({"latitude": 33.41, "longitude": -111.83, "jurisdiction": "mesa_az"})
    assert ok == {"lat": 33.41, "lon": -111.83, "precision": geo.PRECISION_SOURCE}


# ---------------------------------------------------------------- map API

@pytest.fixture()
def placed(world):
    """Give the plumber's water-heater permit and the dealer's racking permit
    real coordinates; leave the blank-scope permit unplaced."""
    c = world["conn"]
    c.execute("UPDATE permits SET latitude=33.41, longitude=-111.83 WHERE description LIKE 'Replace 50 gal%'")
    c.execute("UPDATE permits SET latitude=33.42, longitude=-111.84 WHERE description LIKE 'Install 16 feet%'")
    c.commit()
    return world


def _all_feed_ids(c, user, filters):
    return {i["project_id"] for i in crm.opportunities(c, user, filters, all_rows=True)["items"]}


@pytest.mark.parametrize("ctx", ["assigned", "organization"])
@pytest.mark.parametrize("scope", ["", "all"])
def test_map_and_feed_are_the_same_set(placed, ctx, scope):
    c, admin = placed["conn"], placed["admin"]
    filters = {"context": ctx, "scope": scope}
    m = crm.opportunity_map(c, admin, filters)
    feed = crm.opportunities(c, admin, filters)
    assert m["total"] == feed["total"]
    assert m["placed"] + m["unplaced"] + m["not_loaded"] == m["total"]
    assert m["not_loaded"] == 0 and m["scan_limit"] is None  # under the cap
    assert {p["project_id"] for p in m["points"]} <= _all_feed_ids(c, admin, filters)
    assert m["context"] == feed["context"] == ctx


@pytest.mark.parametrize("scope", ["", "all"])
def test_scan_cap_is_consistent_and_reported(placed, monkeypatch, scope):
    """Above the scan cap, feed and map still agree on the total and both say
    that only the top-scoring candidates were checked or mapped."""
    monkeypatch.setattr(crm, "_SCOPE_SCAN_LIMIT", 2)
    c, admin = placed["conn"], placed["admin"]
    filters = {"context": "organization", "scope": scope}
    m = crm.opportunity_map(c, admin, filters)
    feed = crm.opportunities(c, admin, filters)
    assert m["total"] == feed["total"]
    assert m["placed"] + m["unplaced"] + m["not_loaded"] == m["total"]
    assert m["scan_limit"] == 2 and m["candidates"] > 2
    if scope == "all":
        assert m["not_loaded"] == m["total"] - 2
    else:
        assert feed["scan_limit"] == 2 and m["not_loaded"] == 0


def test_map_respects_trust_filters(placed):
    c, admin = placed["conn"], placed["admin"]
    default = crm.opportunity_map(c, admin, {"context": "organization"})
    assert placed["dealer"] not in {p["company_id"] for p in default["points"]}
    assert placed["co1"] in {p["company_id"] for p in default["points"]}
    assert default["unplaced"] >= 1  # the blank-scope permit has no coordinates
    everything = crm.opportunity_map(c, admin, {"context": "organization", "scope": "all"})
    assert placed["dealer"] in {p["company_id"] for p in everything["points"]}


def test_map_permissions(placed):
    c = placed["conn"]
    with pytest.raises(AuthzError):
        crm.opportunity_map(c, placed["rep"], {"context": "organization"})
    rep = crm.opportunity_map(c, placed["rep"], {"context": "assigned", "scope": "all"})
    assert {p["company_id"] for p in rep["points"]} <= {placed["co1"], placed["dealer"]}


def test_map_empty_reason_passes_through(env):
    c = env["conn"]
    c.execute("DELETE FROM crm_company_relationships")
    c.commit()
    m = crm.opportunity_map(c, env["admin"], {"context": "assigned"})
    assert m["total"] == 0 and m["empty_reason"] == "NO_RELATIONSHIPS" and m["points"] == []


def test_map_points_carry_the_verified_contact(placed):
    c = placed["conn"]
    _chan(c, placed["co1"], "business_phone", "602-555-0130", name="Pat Buyer", title="Purchasing")
    m = crm.opportunity_map(c, placed["mgr"], {"context": "assigned"})
    pt = next(p for p in m["points"] if p["company_id"] == placed["co1"])
    page = verified_contact_view(c, placed["co1"])
    assert pt["contact"]["phone"] == page["phone"]["value"] and pt["contact"]["status"] == "VERIFIED"


def test_map_and_feed_leak_nothing_internal(placed):
    c = placed["conn"]
    c.execute("UPDATE permits SET raw_source_json='{\"SECRET_FIELD\": 1}' WHERE latitude IS NOT NULL")
    c.commit()
    for payload in (crm.opportunity_map(c, placed["admin"], {"context": "organization", "scope": "all"}),
                    crm.opportunities(c, placed["admin"], {"context": "organization", "scope": "all"})):
        keys = set(_keys(payload))
        for banned in (*BANNED_PUBLIC_FIELDS, "raw_source_json", "latitude", "longitude", "gate_result"):
            assert banned not in keys, banned
        assert "SECRET_FIELD" not in json.dumps(payload)
        assert not re.search(r'"roc"|VERIFIED_MATCH|account_priority', json.dumps(payload))


def test_todays_cards_carry_location_or_none(tmp_path):
    import sqlite3

    from agents.ceo.evals import morning_fixture as F
    from pipeline.trust import account_view as trust

    trust.clear_cache()
    db = F.build(tmp_path / "m.db")
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    items = trust.todays_accounts(c, as_of=F.AS_OF)["items"]
    assert items and all("location" in i["project"] for i in items)
    assert all(i["project"]["location"] is None for i in items)  # fixture permits have no coordinates
    c.close()
