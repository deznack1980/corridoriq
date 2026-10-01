"""Shared, single-flight cache for the opportunity scan.

The dashboard asks for the opportunity list and the opportunity map at the same
moment. Both need the same candidate scan (count, top rows by score, trade-scope
classification, account relevance, published locations). This cache runs that
scan once per data signature and hands both requests the same result.

The key always contains a data signature, so a refresh, a new permit, a lane
rebuild, or a CRM relationship change produces a new key: cached results never
differ from what an uncached scan would return. In-memory databases are never
cached.
"""

from __future__ import annotations

import os
import sqlite3

from pipeline.trust.singleflight import SingleFlightCache

MAX_ENTRIES = 16

_cache = SingleFlightCache(MAX_ENTRIES)
stats = _cache.stats


def clear() -> None:
    _cache.clear()


def peek(key: tuple) -> bool:
    return _cache.peek(key)


def db_file(conn: sqlite3.Connection) -> str | None:
    """Path of the main database file, or None for in-memory / temporary DBs."""
    try:
        path = conn.execute("PRAGMA database_list").fetchone()[2]
    except sqlite3.Error:
        return None
    # Normalized so URI (read-only) and plain-path connections share keys.
    return os.path.normcase(os.path.abspath(path)) if path else None


def data_signature(conn: sqlite3.Connection, org_id: int) -> tuple | None:
    """What the scan depends on. None means "do not cache"."""
    path = db_file(conn)
    if path is None:
        return None
    one = lambda sql, *a: conn.execute(sql, a).fetchone()  # noqa: E731
    try:
        run = one("SELECT id, completed_at FROM pipeline_runs WHERE status IN "
                  "('succeeded','success','completed') ORDER BY id DESC LIMIT 1")
        rel = one("SELECT COUNT(*), MAX(updated_at), MAX(id) FROM crm_company_relationships "
                  "WHERE organization_id=?", org_id)
        return (
            path,
            None if run is None else tuple(run),
            one("SELECT MAX(id) FROM permits")[0],
            one("SELECT MAX(id) FROM projects")[0],
            one("SELECT MAX(id) FROM companies")[0],
            tuple(one("SELECT MAX(created_at), COUNT(*) FROM company_sales_lanes")),
            tuple(rel),
        )
    except sqlite3.Error:
        return None


def get_or_compute(key: tuple | None, compute):
    """Return the cached value for key, computing it once. Concurrent callers
    with the same key wait for the first computation instead of repeating it.
    Callers must not mutate the returned value."""
    return _cache.get_or_compute(key, compute)
