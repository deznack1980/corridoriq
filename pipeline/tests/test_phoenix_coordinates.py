"""Phoenix connector geometry and the coordinate backfill."""

from __future__ import annotations

import json
import sqlite3

import pytest

from pipeline.connectors import arcgis_hub as hub
from pipeline.ingestion import phoenix_coordinates as pc


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _feature(num, x, y, **attrs):
    return {"attributes": {"PER_NUM": num, "STREET_FULL_NAME": "1 W TEST ST", "OBJECTID": 7, **attrs},
            "geometry": None if x is None else {"x": x, "y": y}}


def test_connector_requests_wgs84_points_and_maps_them(monkeypatch):
    conn = hub.build_phoenix_connector()
    seen = {}

    def fake_get(url, params, timeout):
        seen.update(params)
        return _Resp({"features": [_feature("T1", -112.1853, 33.4843), _feature("T2", None, None),
                                   _feature("T3", 500000.0, 900000.0)]})

    monkeypatch.setattr(conn.session, "get", fake_get)
    raws = list(conn.fetch_raw())
    assert seen["returnGeometry"] == "true" and seen["outSR"] == "4326"
    mapped = [conn.map_record(r) for r in raws]
    assert (mapped[0]["latitude"], mapped[0]["longitude"]) == (33.4843, -112.1853)
    assert mapped[1]["latitude"] is None and mapped[1]["longitude"] is None  # no geometry
    assert mapped[2]["latitude"] is None and mapped[2]["longitude"] is None  # not degrees: nulled


def test_raw_payload_is_unchanged_by_geometry():
    """raw_source_json (and so the RAW hash and change detection) stays the
    attribute record exactly as before geometry was requested."""
    attrs = {"PER_NUM": "T1", "STREET_FULL_NAME": "1 W TEST ST", "OBJECTID": 7}
    feat = hub.EsriFeature(dict(attrs), {"x": -112.1, "y": 33.4})
    assert json.dumps(feat, default=str) == json.dumps(attrs, default=str)
    assert feat.geometry == {"x": -112.1, "y": 33.4}


def test_other_connectors_do_not_request_geometry(monkeypatch):
    conn = hub.build_tempe_connector()
    seen = {}
    monkeypatch.setattr(conn.session, "get", lambda url, params, timeout: (seen.update(params), _Resp({"features": []}))[1])
    list(conn.fetch_raw())
    assert "returnGeometry" not in seen and "outSR" not in seen


@pytest.fixture()
def db():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE permits (id INTEGER PRIMARY KEY, jurisdiction TEXT, permit_number TEXT, "
              "latitude REAL, longitude REAL, raw_source_json TEXT, last_updated_at TEXT)")
    rows = [
        ("phoenix_az", "A", None, None), ("phoenix_az", "B", 33.5, -112.0), ("phoenix_az", "C", 33.6, -112.2),
        ("phoenix_az", "D", None, None), ("phoenix_az", "E", None, None), ("phoenix_az", "F", None, None),
        ("mesa_az", "A", None, None),
    ]
    c.executemany("INSERT INTO permits (jurisdiction, permit_number, latitude, longitude, raw_source_json, "
                  "last_updated_at) VALUES (?,?,?,?,'{}','2026-09-01')", rows)
    c.commit()
    return c


def _pages(features):
    return lambda offset: features if offset == 0 else []


def test_plan_fills_only_empty_and_never_guesses(db):
    features = [
        _feature("A", -112.10, 33.45),                              # fill
        _feature("B", -112.0, 33.5),                                # already equal
        _feature("C", -112.9, 33.9),                                # differs: not overwritten
        _feature("D", -112.1, 33.4), _feature("D", -112.3, 33.7),   # ambiguous: skipped
        _feature("E", None, None),                                  # no valid point
        # F: not in source
    ]
    plan = pc.plan(db, pc.source_points(_pages(features)))
    assert plan["to_fill"] == 1 and plan["updates"] == [(33.45, -112.10, "A")]
    assert plan["already_equal"] == 1
    assert plan["existing_differs_not_overwritten"] == 1
    assert plan["skipped_ambiguous"] == 1
    assert plan["source_without_valid_point"] == 1
    assert plan["not_in_source"] == 1


def test_apply_writes_only_phoenix_empty_rows_and_leaves_timestamps(db):
    written = pc.apply(db, [(33.45, -112.10, "A"), (1.0, 1.0, "B")])
    assert written == 1  # B already had coordinates: untouched
    got = dict(((r[0], r[1]), (r[2], r[3], r[4], r[5])) for r in db.execute(
        "SELECT jurisdiction, permit_number, latitude, longitude, raw_source_json, last_updated_at FROM permits"))
    assert got[("phoenix_az", "A")] == (33.45, -112.10, "{}", "2026-09-01")
    assert got[("phoenix_az", "B")][:2] == (33.5, -112.0)
    assert got[("mesa_az", "A")][:2] == (None, None)
