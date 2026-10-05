"""Read-only execution for approved SELECT statements.

Security is the SQLite authorizer plus mode=ro and PRAGMA query_only.
A keyword check is a second gate, not the only one.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from agents.ceo.analytics.limits import PROGRESS_STEPS
from agents.ceo.operating_state.collector import connect_readonly

SQLITE_OK = 0
SQLITE_DENY = 1
# SQLITE_READ, SQLITE_SELECT, SQLITE_FUNCTION, SQLITE_TRANSACTION
_ALLOW = {20, 21, 22, 31}

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH|VACUUM|PRAGMA)\b",
    re.IGNORECASE,
)


class AnalyticsError(Exception):
    """Fail closed. Callers turn this into UNKNOWN, not a guess."""


def open_analytics(path: Path) -> sqlite3.Connection:
    if path is None or not Path(path).exists():
        raise AnalyticsError("database unavailable")
    try:
        conn = connect_readonly(Path(path))
    except (OSError, sqlite3.Error) as exc:
        raise AnalyticsError("database unavailable") from exc
    steps = {"n": 0}

    def _progress() -> int:
        steps["n"] += 1
        if steps["n"] > PROGRESS_STEPS:
            return 1
        return 0

    def _authorizer(action, arg1, arg2, db_name, source) -> int:
        if action in _ALLOW:
            return SQLITE_OK
        return SQLITE_DENY

    conn.set_authorizer(_authorizer)
    conn.set_progress_handler(_progress, 1000)
    return conn


def run_select(conn: sqlite3.Connection, sql: str, params: tuple = (), *, limit: int | None = None) -> list[dict]:
    statement = sql.strip()
    if not statement.upper().startswith("SELECT"):
        raise AnalyticsError("only SELECT is allowed")
    if _FORBIDDEN.search(statement):
        raise AnalyticsError("statement is not a read")
    body = statement.rstrip().rstrip(";")
    if ";" in body:
        raise AnalyticsError("multiple statements are not allowed")
    try:
        cursor = conn.execute(body, params)
        rows = cursor.fetchmany(limit) if limit is not None else cursor.fetchall()
    except sqlite3.Error as exc:
        raise AnalyticsError("analytics query failed") from exc
    return [dict(row) for row in rows]
