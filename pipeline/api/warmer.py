"""Background cache warmer for the portal server.

The morning refresh may run in a different process, so it cannot fill this
server's in-memory caches directly. Instead the server checks the data
signature once a minute; when a refresh finishes (or the day rolls over, or
the server starts) it pre-computes, off the request path:

- the daily trust queue (Today's Accounts), and
- the organization opportunity scan shared by the map and the list.

Nothing here writes to the database. Results are the same objects a request
would have computed; the warmer only moves the cost to before the first visit.
Disable with CORRIDORIQ_CACHE_WARMER=0.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

POLL_SECONDS = 60

_ORG_SCOPE_PERMISSIONS = frozenset({"projects.view_assigned", "companies.view"})


def enabled() -> bool:
    return os.environ.get("CORRIDORIQ_CACHE_WARMER", "1") != "0"


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def signature(conn: sqlite3.Connection) -> tuple:
    """Changes when either cache would miss: a new refresh or lane build, the
    Phoenix day (queue), or the UTC day that moves the 60-day scan window."""
    from pipeline.crm import scan_cache
    from pipeline.crm.service import _window_start
    from pipeline.trust.account_view import _cache_key
    from pipeline.trust.dates import phoenix_now

    org_ids = tuple(r[0] for r in conn.execute("SELECT id FROM organizations ORDER BY id"))
    return (_cache_key(conn, phoenix_now()), _window_start(),
            tuple(scan_cache.data_signature(conn, o) for o in org_ids))


def warm_once(db_path: Path, last: tuple | None = None) -> tuple[tuple, dict]:
    """Warm both caches if the data signature changed. Returns (signature, report)."""
    from pipeline.crm import service as crm
    from pipeline.trust import account_view

    conn = _connect(db_path)
    try:
        sig = signature(conn)
        if sig == last:
            return sig, {"warmed": False}
        report = {"warmed": True, "queue_seconds": None, "scan_seconds": {}}
        t = time.time()
        account_view.daily_queue(conn)
        report["queue_seconds"] = round(time.time() - t, 2)
        for (org_id,) in conn.execute("SELECT id FROM organizations"):
            # An organization-scope context yields the same scan key that
            # admins and managers use; its response is discarded.
            ctx = {"id": 0, "organization_id": org_id, "permissions": set(_ORG_SCOPE_PERMISSIONS)}
            t = time.time()
            crm.opportunities(conn, ctx, {"context": "organization"}, all_rows=True)
            report["scan_seconds"][org_id] = round(time.time() - t, 2)
        return sig, report
    finally:
        conn.close()


class CacheWarmer(threading.Thread):
    def __init__(self, db_path: Path, poll_seconds: int = POLL_SECONDS, log=print):
        super().__init__(name="corridoriq-cache-warmer", daemon=True)
        self.db_path = Path(db_path)
        self.poll_seconds = poll_seconds
        self.log = log
        self.last: tuple | None = None
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.last, report = warm_once(self.db_path, self.last)
                if report.get("warmed"):
                    self.log(f"[cache-warmer] warmed: queue {report['queue_seconds']}s, "
                             f"scan {report['scan_seconds']}")
            except Exception as exc:  # never let warming take the server down
                self.log(f"[cache-warmer] skipped: {type(exc).__name__}: {exc}")
            self._stop.wait(self.poll_seconds)
