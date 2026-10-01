"""Probe an ArcGIS layer to isolate why a jurisdiction is failing.

Read-only. Distinguishes "the service is down" from "our query is wrong",
which the pipeline's error message alone cannot tell you.

    python scripts/diagnose_source.py gilbert_az
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config.settings import HTTP_USER_AGENT  # noqa: E402
from pipeline.connectors.registry import build_connector  # noqa: E402

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": HTTP_USER_AGENT})


def _get(url: str, params: dict) -> tuple[int, object]:
    try:
        r = SESSION.get(url, params=params, timeout=30)
    except requests.RequestException as exc:
        return 0, f"{type(exc).__name__}: {exc}"
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def _short(body) -> str:
    if isinstance(body, dict):
        if "error" in body:
            return f"ERROR {json.dumps(body['error'])[:220]}"
        if "count" in body:
            return f"count={body['count']}"
        if "features" in body:
            return f"features={len(body['features'])}"
        return f"keys={sorted(body)[:8]}"
    return str(body)[:220]


def probe(slug: str) -> int:
    connector = build_connector(slug, "arcgis_hub")
    base = connector.service_url
    date_field = connector.date_field
    print("=" * 72)
    print(f" Probing {slug}")
    print("=" * 72)
    print(f"service    : {base}")
    print(f"date_field : {date_field}")

    print("\n[1] layer metadata")
    code, body = _get(base, {"f": "json"})
    print(f"    HTTP {code}  {_short(body)}")
    if isinstance(body, dict) and "fields" in body:
        print(f"    name={body.get('name')!r} type={body.get('type')!r}")
        print(f"    maxRecordCount={body.get('maxRecordCount')}")
        if date_field:
            match = [f for f in body["fields"] if f["name"] == date_field]
            if match:
                print(f"    {date_field} -> type={match[0]['type']}")
            else:
                names = [f["name"] for f in body["fields"]]
                print(f"    !! {date_field} NOT in layer fields: {names[:12]}")

    print("\n[2] unfiltered count (is the service alive at all?)")
    code, body = _get(f"{base}/query",
                      {"where": "1=1", "returnCountOnly": "true", "f": "json"})
    print(f"    HTTP {code}  {_short(body)}")

    if not date_field:
        print("\n(no date_field configured - full refresh only)")
        return 0

    since = "2026-09-15 07:50:35"
    print(f"\n[3] the query the pipeline actually sends")
    code, body = _get(f"{base}/query", {
        "where": f"{date_field} >= TIMESTAMP '{since}'",
        "outFields": "*", "f": "json",
        "resultOffset": 0, "resultRecordCount": 1000,
    })
    print(f"    HTTP {code}  {_short(body)}")

    print("\n[4] same filter, count only (isolates outFields/paging)")
    code, body = _get(f"{base}/query", {
        "where": f"{date_field} >= TIMESTAMP '{since}'",
        "returnCountOnly": "true", "f": "json",
    })
    print(f"    HTTP {code}  {_short(body)}")

    print("\n[5] same filter, no pagination params")
    code, body = _get(f"{base}/query", {
        "where": f"{date_field} >= TIMESTAMP '{since}'",
        "outFields": "*", "f": "json",
    })
    print(f"    HTTP {code}  {_short(body)}")

    print("\n[6] alternative: epoch-millis comparison instead of TIMESTAMP")
    epoch_ms = 1789458635000  # 2026-09-15 07:50:35 UTC
    code, body = _get(f"{base}/query", {
        "where": f"{date_field} >= {epoch_ms}",
        "returnCountOnly": "true", "f": "json",
    })
    print(f"    HTTP {code}  {_short(body)}")

    print("\n[7] alternative: server-side time filter via `time` parameter")
    code, body = _get(f"{base}/query", {
        "where": "1=1", "time": f"{epoch_ms},null",
        "returnCountOnly": "true", "f": "json",
    })
    print(f"    HTTP {code}  {_short(body)}")

    print("\n[8] an older watermark (is it the date, or the filter syntax?)")
    code, body = _get(f"{base}/query", {
        "where": f"{date_field} >= TIMESTAMP '2026-01-01 00:00:00'",
        "returnCountOnly": "true", "f": "json",
    })
    print(f"    HTTP {code}  {_short(body)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("slug")
    return probe(parser.parse_args().slug)


if __name__ == "__main__":
    raise SystemExit(main())
