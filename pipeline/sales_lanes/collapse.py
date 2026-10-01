"""Canonical presentation collapse. Never merges source companies."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from pipeline.entity.names import compact_company_name


def canonical_map(conn: sqlite3.Connection) -> dict[int, int]:
    """raw company_id -> canonical_companies.id when a link exists."""
    out = {}
    for row in conn.execute(
        """
        SELECT raw_company_id, canonical_company_id
        FROM company_entity_links
        """
    ):
        out[int(row["raw_company_id"])] = int(row["canonical_company_id"])
    return out


def canonical_names(conn: sqlite3.Connection) -> dict[int, str]:
    return {
        int(r["id"]): r["canonical_name"]
        for r in conn.execute("SELECT id, canonical_name FROM canonical_companies")
    }


def presentation_groups(
    conn: sqlite3.Connection,
    company_ids: list[int],
    *,
    names: dict[int, str] | None = None,
) -> dict[str, list[int]]:
    """Group raw ids. Prefer canonical links, else compact name within this set."""
    cmap = canonical_map(conn)
    cnames = canonical_names(conn)
    names = names or {}
    missing = [cid for cid in company_ids if cid not in names]
    if missing:
        placeholders = ",".join("?" * len(missing))
        for row in conn.execute(
            f"SELECT id, display_name FROM companies WHERE id IN ({placeholders})",
            missing,
        ):
            names[int(row["id"])] = row["display_name"]
    groups: dict[str, list[int]] = defaultdict(list)
    compact_index: dict[str, str] = {}
    for cid in company_ids:
        if cid in cmap:
            key = f"canonical:{cmap[cid]}"
            groups[key].append(cid)
            continue
        compact = compact_company_name(names.get(cid) or "")
        if compact and compact in compact_index:
            groups[compact_index[compact]].append(cid)
            continue
        key = f"raw:{cid}"
        if compact:
            compact_index[compact] = key
        groups[key].append(cid)
    return dict(groups)


def collapse_stats(conn: sqlite3.Connection, company_ids: list[int], names: dict[int, str]) -> dict:
    groups = presentation_groups(conn, company_ids, names=names)
    raw = len(company_ids)
    unique = len(groups)
    dupes = [members for members in groups.values() if len(members) > 1]
    return {
        "raw_rows": raw,
        "presentation_rows": unique,
        "duplicate_rows_removed": raw - unique,
        "collapsed_clusters": len(dupes),
        "largest_cluster": max((len(m) for m in dupes), default=1),
    }
