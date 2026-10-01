"""One-off backfill of Phoenix permit coordinates from the city's own layer.

Phoenix's Planning_Permit layer is a point layer. The connector used to keep
only attributes, so every stored Phoenix permit has NULL latitude/longitude.
The connector now requests WGS84 point geometry; this module fills the
existing rows from the same source, using the connector's own mapping
functions so backfilled and newly ingested values are identical.

Rules:
- Only rows whose latitude AND longitude are both NULL are written.
- A permit number that appears with different points in the source is
  ambiguous and skipped.
- Values outside Arizona are dropped by the connector's validators.
- raw_source_json and last_updated_at are not touched: coordinates do not feed
  analysis, so the refresh must not re-analyze 50k rows because of this.
"""

from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Callable, Iterable

from pipeline.connectors.arcgis_hub import (
    ESRI_PAGE_SIZE,
    PHOENIX_FIELD_MAP,
    PHOENIX_SERVICE_URL,
    EsriFeature,
)

JURISDICTION = "phoenix_az"


def source_points(fetch_page: Callable[[int], list[dict]]) -> Iterable[tuple[str, float | None, float | None]]:
    """(permit_number, lat, lon) for every feature, via the connector mapping."""
    lat_of, lon_of = PHOENIX_FIELD_MAP["latitude"], PHOENIX_FIELD_MAP["longitude"]
    offset = 0
    while True:
        features = fetch_page(offset)
        if not features:
            return
        for f in features:
            feat = EsriFeature(f.get("attributes", {}), f.get("geometry"))
            num = feat.get("PER_NUM")
            if num:
                yield str(num), lat_of(feat), lon_of(feat)
        if len(features) < ESRI_PAGE_SIZE:
            return
        offset += ESRI_PAGE_SIZE


def http_fetch_page(session, delay: float) -> Callable[[int], list[dict]]:
    def fetch(offset: int) -> list[dict]:
        if offset:
            time.sleep(delay)
        resp = session.get(f"{PHOENIX_SERVICE_URL}/query", timeout=60, params={
            "where": "1=1", "outFields": "PER_NUM", "returnGeometry": "true", "outSR": "4326",
            "orderByFields": "OBJECTID", "resultOffset": offset,
            "resultRecordCount": ESRI_PAGE_SIZE, "f": "json",
        })
        resp.raise_for_status()
        payload = resp.json()
        if "error" in payload:
            raise RuntimeError(f"ArcGIS query error: {payload['error']}")
        return payload.get("features", [])
    return fetch


def plan(conn: sqlite3.Connection, points: Iterable[tuple[str, float | None, float | None]]) -> dict:
    by_number: dict[str, set] = defaultdict(set)
    features = 0
    for num, lat, lon in points:
        features += 1
        by_number[num].add((lat, lon))
    ambiguous = {n for n, pts in by_number.items() if len(pts) > 1}
    usable = {n: next(iter(pts)) for n, pts in by_number.items()
              if n not in ambiguous and None not in next(iter(pts))}
    no_point = {n for n, pts in by_number.items() if n not in ambiguous and n not in usable}

    rows = conn.execute(
        "SELECT permit_number, latitude, longitude FROM permits WHERE jurisdiction = ?", (JURISDICTION,)
    ).fetchall()
    fill, already, differs, missing_in_source, skipped_ambiguous, source_no_point = [], 0, 0, 0, 0, 0
    for num, lat, lon in rows:
        if num in ambiguous:
            skipped_ambiguous += 1
        elif num not in by_number:
            missing_in_source += 1
        elif num in no_point:
            source_no_point += 1
        elif lat is None and lon is None:
            fill.append((usable[num][0], usable[num][1], num))
        elif (lat, lon) == usable[num]:
            already += 1
        else:
            differs += 1  # existing coordinates are never overwritten
    return {
        "source_features": features,
        "source_permit_numbers": len(by_number),
        "source_ambiguous_numbers": len(ambiguous),
        "permits": len(rows),
        "to_fill": len(fill),
        "already_equal": already,
        "existing_differs_not_overwritten": differs,
        "not_in_source": missing_in_source,
        "source_without_valid_point": source_no_point,
        "skipped_ambiguous": skipped_ambiguous,
        "updates": fill,
    }


def apply(conn: sqlite3.Connection, updates: list[tuple[float, float, str]]) -> int:
    """Fill empty coordinates in one transaction. Returns rows written."""
    cur = conn.cursor()
    if not conn.in_transaction:
        cur.execute("BEGIN IMMEDIATE")
    before = conn.total_changes
    try:
        cur.executemany(
            "UPDATE permits SET latitude = ?, longitude = ? "
            "WHERE jurisdiction = 'phoenix_az' AND permit_number = ? "
            "AND latitude IS NULL AND longitude IS NULL",
            updates,
        )
        written = conn.total_changes - before
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return written
