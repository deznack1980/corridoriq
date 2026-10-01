"""Read-only operating metrics. Unavailable metrics stay UNKNOWN."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from agents.ceo.operating_state.queries import SELECTS
from agents.framework.provenance import VERIFIED_SYSTEM_STATE, metric, unknown

_DB_SOURCE = "sqlite-readonly"


def connect_readonly(path: Path) -> sqlite3.Connection:
    """Open SQLite without creating a file and without allowing writes."""
    if not path.exists():
        raise FileNotFoundError(path)
    uri = path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    conn.execute("PRAGMA busy_timeout = 3000")
    return conn


def resolve_db_path(explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    try:
        from pipeline.config.settings import DB_PATH
    except Exception:
        return None
    return Path(DB_PATH)


def collect_database(db_path: Path | None) -> dict:
    """Return named metrics. Never writes. Never raises for a missing database."""
    if db_path is None or not db_path.exists():
        note = "database not available for a read-only snapshot"
        return {name: unknown(note, source=_DB_SOURCE) for name in SELECTS}
    try:
        conn = connect_readonly(db_path)
    except Exception:
        note = "database not readable"
        return {name: unknown(note, source=_DB_SOURCE) for name in SELECTS}
    try:
        return {name: _run(conn, name, sql) for name, sql in SELECTS.items()}
    finally:
        conn.close()


def _run(conn: sqlite3.Connection, name: str, sql: str) -> dict:
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error:
        return unknown(f"{name} unavailable", source=_DB_SOURCE)
    if name in {"source_health", "sales_lane_counts"}:
        value = {str(row[0]): int(row[1]) for row in rows}
        return metric(value, source=_DB_SOURCE, category=VERIFIED_SYSTEM_STATE)
    if name == "latest_pipeline_run":
        if not rows:
            return unknown("no pipeline_runs rows", source=_DB_SOURCE)
        row = rows[0]
        return metric(
            {
                "status": row["status"],
                "started_at": row["started_at"],
                "completed_at": row["completed_at"],
                "run_type": row["run_type"],
            },
            source=_DB_SOURCE,
            category=VERIFIED_SYSTEM_STATE,
            timestamp=row["started_at"],
        )
    if not rows or rows[0][0] is None:
        return unknown(f"no {name} row", source=_DB_SOURCE)
    value = rows[0][0]
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    elif isinstance(value, int):
        value = int(value)
    return metric(value, source=_DB_SOURCE, category=VERIFIED_SYSTEM_STATE)
