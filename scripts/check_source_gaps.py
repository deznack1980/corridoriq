"""Compare what each source publishes against what CorridorIQ actually holds.

Data platform Phase 2 diagnostic. The in-database health rollup
(`pipeline.telemetry.source_health`) can tell you a source has gone quiet, but
not whether the source is quiet or we are simply failing to collect from it.
Answering that needs a network call, which is why it lives here rather than in
the snapshot that runs after every ingest.

Read-only. Makes no changes to the database or to any source.

    python scripts/check_source_gaps.py
    python scripts/check_source_gaps.py --slug scottsdale_az
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.config.settings import DB_PATH, HTTP_USER_AGENT  # noqa: E402
from pipeline.connectors.base import ConnectorNotConfiguredError  # noqa: E402
from pipeline.connectors.registry import build_connector  # noqa: E402

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": HTTP_USER_AGENT})


def _ms_to_day(value) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError, OSError):
        return str(value)[:10]


def _arcgis_newest(connector) -> str | None:
    """Newest value of the incremental date field.

    Tries outStatistics first, then falls back to an ordered single-row query:
    some older MapServers accept outStatistics but return it without the
    requested alias, and Phoenix's layer is one of them.
    """
    field = connector.date_field
    stats = [{"statisticType": "max", "onStatisticField": field,
              "outStatisticFieldName": "mx"}]
    r = SESSION.get(f"{connector.service_url}/query",
                    params={"where": "1=1", "outStatistics": json.dumps(stats),
                            "f": "json"}, timeout=30)
    body = r.json()
    if "error" not in body:
        features = body.get("features") or []
        if features:
            attrs = features[0].get("attributes", {})
            # The alias is not always honoured; take it however it comes back.
            value = attrs.get("mx")
            if value is None and len(attrs) == 1:
                value = next(iter(attrs.values()))
            if value is not None:
                return _ms_to_day(value)

    r = SESSION.get(f"{connector.service_url}/query", params={
        "where": f"{field} IS NOT NULL", "outFields": field,
        "orderByFields": f"{field} DESC", "resultRecordCount": 1,
        "returnGeometry": "false", "f": "json"}, timeout=30)
    body = r.json()
    if "error" in body:
        raise RuntimeError(str(body["error"])[:120])
    features = body.get("features") or []
    if not features:
        return None
    return _ms_to_day(features[0].get("attributes", {}).get(field))


def _arcgis_rows_after(connector, day: str) -> int | None:
    r = SESSION.get(f"{connector.service_url}/query", params={
        "where": f"{connector.date_field} > TIMESTAMP '{day} 23:59:59'",
        "returnCountOnly": "true", "f": "json"}, timeout=30)
    body = r.json()
    if "error" in body:
        raise RuntimeError(str(body["error"])[:120])
    return body.get("count")


def _socrata_newest(connector) -> str | None:
    r = SESSION.get(connector.resource_url,
                    params={"$select": f"max({connector.date_field}) AS mx"},
                    timeout=30)
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        return None
    value = rows[0].get("mx") or next(iter(rows[0].values()), None)
    return str(value)[:10] if value else None


def _socrata_rows_after(connector, day: str) -> int | None:
    r = SESSION.get(connector.resource_url, params={
        "$select": "count(1) AS n",
        "$where": f"{connector.date_field} > '{day}T23:59:59'"}, timeout=30)
    rows = r.json()
    if not isinstance(rows, list) or not rows:
        return None
    value = rows[0].get("n") or next(iter(rows[0].values()), None)
    return int(value) if value is not None else None


def check(slug: str, connector_type: str, conn) -> dict:
    out = {"slug": slug, "held": 0, "held_newest": None,
           "source_newest": None, "missing": None, "note": None}

    row = conn.execute(
        "SELECT COUNT(*) n, MAX(COALESCE(issued_date, filed_date)) mx "
        "FROM permits WHERE jurisdiction = ?", (slug,)).fetchone()
    out["held"] = row["n"]
    # Stored dates are inconsistent across sources: some are 'YYYY-MM-DD',
    # Mesa's are full ISO timestamps. Compare on the day only.
    out["held_newest"] = str(row["mx"])[:10] if row["mx"] else None

    try:
        connector = build_connector(slug, connector_type)
    except ConnectorNotConfiguredError as exc:
        out["note"] = f"not configured: {exc}"
        return out

    if not getattr(connector, "date_field", None):
        out["note"] = "full refresh (no incremental date field)"
        return out

    try:
        if connector_type == "socrata":
            out["source_newest"] = _socrata_newest(connector)
            if out["held_newest"]:
                out["missing"] = _socrata_rows_after(connector, out["held_newest"])
        else:
            out["source_newest"] = _arcgis_newest(connector)
            if out["held_newest"]:
                out["missing"] = _arcgis_rows_after(connector, out["held_newest"])
    except Exception as exc:  # noqa: BLE001
        out["note"] = f"{type(exc).__name__}: {exc}"[:110]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slug")
    args = parser.parse_args()

    with closing(sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        sql = ("SELECT slug, connector_type FROM jurisdictions "
               "WHERE status='connected'")
        params: tuple = ()
        if args.slug:
            sql += " AND slug = ?"
            params = (args.slug,)
        sources = list(conn.execute(sql + " ORDER BY slug", params))

        print("=" * 84)
        print(" CorridorIQ - source coverage gaps")
        print("=" * 84)
        print(f"  {'source':<16}{'held':>8}  {'our newest':<12}{'source newest':<14}"
              f"{'missing':>8}  note")

        total_missing = 0
        for row in sources:
            r = check(row["slug"], row["connector_type"], conn)
            missing = r["missing"]
            if isinstance(missing, int):
                total_missing += missing
            print(f"  {r['slug']:<16}{r['held']:>8,}  "
                  f"{str(r['held_newest'] or '-'):<12}"
                  f"{str(r['source_newest'] or '-'):<14}"
                  f"{('-' if missing is None else format(missing, ',')):>8}  "
                  f"{r['note'] or ''}")

        print(f"\n  Records published by sources but never collected: {total_missing:,}")
        if total_missing:
            print("  A non-zero figure means the incremental watermark is skipping"
                  " records,\n  not that the sources are quiet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
