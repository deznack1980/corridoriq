"""Permit locations for the opportunity map. Read-only; nothing is geocoded.

A permit is placed only from coordinates its source actually published:

- permits.latitude / permits.longitude (Mesa, Tempe, Gilbert, Scottsdale), or
- Peoria's X_COORD / Y_COORD in the stored raw record, which are Arizona
  Central State Plane (NAD83, international feet, EPSG:2223), converted here.

Everything else (Phoenix and Buckeye today) is reported as not placeable. No
city-centroid or address guess is ever returned as a location.
"""

from __future__ import annotations

import json
import math

# Phoenix metro sanity box. A coordinate outside it is treated as bad data.
METRO_BOUNDS = (32.90, -113.10, 34.20, -111.30)  # south, west, north, east

PRECISION_SOURCE = "source_coordinates"
PRECISION_STATE_PLANE = "source_state_plane"

# EPSG:2223 NAD83 / Arizona Central (ft): Transverse Mercator on GRS80.
_A = 6378137.0
_F = 1 / 298.257222101
_E2 = _F * (2 - _F)
_LAT0 = math.radians(31.0)
_LON0 = math.radians(-111.0 - 55.0 / 60.0)  # -111 55' = -111.9166667
_K0 = 0.9999
_FE_M = 213360.0
_FT = 0.3048


def _meridian_arc(phi: float) -> float:
    e2, e4, e6 = _E2, _E2 ** 2, _E2 ** 3
    return _A * (
        (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
        - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
        - (35 * e6 / 3072) * math.sin(6 * phi)
    )


_M0 = _meridian_arc(_LAT0)


def az_central_ft_to_wgs84(x_ft: float, y_ft: float) -> tuple[float, float]:
    """Inverse Transverse Mercator (Snyder, USGS PP 1395, eqs. 8-18..8-25).
    NAD83 and WGS84 differ by about a metre here, below map precision."""
    x = x_ft * _FT - _FE_M
    y = y_ft * _FT
    e2 = _E2
    ep2 = e2 / (1 - e2)
    m = _M0 + y / _K0
    mu = m / (_A * (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256))
    e1 = (1 - math.sqrt(1 - e2)) / (1 + math.sqrt(1 - e2))
    phi1 = (
        mu
        + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
        + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
        + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
        + (1097 * e1 ** 4 / 512) * math.sin(8 * mu)
    )
    s, c, t = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    c1 = ep2 * c * c
    t1 = t * t
    n1 = _A / math.sqrt(1 - e2 * s * s)
    r1 = _A * (1 - e2) / (1 - e2 * s * s) ** 1.5
    d = x / (n1 * _K0)
    lat = phi1 - (n1 * t / r1) * (
        d ** 2 / 2
        - (5 + 3 * t1 + 10 * c1 - 4 * c1 ** 2 - 9 * ep2) * d ** 4 / 24
        + (61 + 90 * t1 + 298 * c1 + 45 * t1 ** 2 - 252 * ep2 - 3 * c1 ** 2) * d ** 6 / 720
    )
    lon = _LON0 + (
        d
        - (1 + 2 * t1 + c1) * d ** 3 / 6
        + (5 - 2 * c1 + 28 * t1 - 3 * c1 ** 2 + 8 * ep2 + 24 * t1 ** 2) * d ** 5 / 120
    ) / c
    return math.degrees(lat), math.degrees(lon)


def wgs84_to_az_central_ft(lat: float, lon: float) -> tuple[float, float]:
    """Forward projection, used only to test the inverse."""
    phi, lam = math.radians(lat), math.radians(lon)
    e2 = _E2
    ep2 = e2 / (1 - e2)
    n = _A / math.sqrt(1 - e2 * math.sin(phi) ** 2)
    t = math.tan(phi) ** 2
    c = ep2 * math.cos(phi) ** 2
    a = (lam - _LON0) * math.cos(phi)
    m = _meridian_arc(phi)
    x = _K0 * n * (a + (1 - t + c) * a ** 3 / 6 + (5 - 18 * t + t ** 2 + 72 * c - 58 * ep2) * a ** 5 / 120)
    y = _K0 * (m - _M0 + n * math.tan(phi) * (
        a ** 2 / 2 + (5 - t + 9 * c + 4 * c ** 2) * a ** 4 / 24
        + (61 - 58 * t + t ** 2 + 600 * c - 330 * ep2) * a ** 6 / 720))
    return (x + _FE_M) / _FT, y / _FT


def _in_metro(lat, lon) -> bool:
    s, w, n, e = METRO_BOUNDS
    return lat is not None and lon is not None and s <= lat <= n and w <= lon <= e


def permit_location(row: dict) -> dict | None:
    """Location published by the permit's source, or None. Needs latitude,
    longitude, jurisdiction and raw_source_json columns on the row."""
    lat, lon = row.get("latitude"), row.get("longitude")
    try:
        lat = None if lat is None else float(lat)
        lon = None if lon is None else float(lon)
    except (TypeError, ValueError):
        lat = lon = None
    if _in_metro(lat, lon):
        return {"lat": round(lat, 6), "lon": round(lon, 6), "precision": PRECISION_SOURCE}
    if row.get("jurisdiction") == "peoria_az" and row.get("raw_source_json"):
        try:
            raw = json.loads(row["raw_source_json"])
            x, y = float(raw.get("X_COORD")), float(raw.get("Y_COORD"))
        except (TypeError, ValueError, json.JSONDecodeError, AttributeError):
            return None
        if x > 0 and y > 0:
            plat, plon = az_central_ft_to_wgs84(x, y)
            if _in_metro(plat, plon):
                return {"lat": round(plat, 6), "lon": round(plon, 6), "precision": PRECISION_STATE_PLANE}
    return None
