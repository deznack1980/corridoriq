"""Connections: production is READ-ONLY; the shadow database is the only
place this package writes.

Production: SQLite URI ``mode=ro`` plus ``PRAGMA query_only=ON`` — any write
raises. Shadow: a path is accepted only if it is not the production database
(or any database that looks like one), ends in ``.db``, and — when it already
exists — carries this package's ``shadow_meta`` stamp. Nothing is created at a
path until those checks pass.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from pipeline.identity_shadow.schema import SHADOW_PURPOSE, SHADOW_SCHEMA


class ShadowPathError(Exception):
    """Refused shadow path. Fail closed."""


def open_production_readonly(db_path) -> sqlite3.Connection:
    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("production database not found")
    uri = "file:" + quote(path.as_posix(), safe="/:") + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _same_file(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def validate_shadow_path(shadow_path, production_path) -> Path:
    if shadow_path is None or str(shadow_path).strip() == "":
        raise ShadowPathError("shadow database path is required")
    shadow = Path(shadow_path).expanduser().resolve()
    prod = Path(production_path).expanduser().resolve() if production_path else None
    if shadow.suffix.lower() != ".db":
        raise ShadowPathError("shadow database must be a .db file")
    if prod is not None and (shadow == prod or _same_file(shadow, prod)):
        raise ShadowPathError("shadow path is the production database")
    if shadow.name.lower() == "corridoriq.db":
        raise ShadowPathError("shadow path uses the production database file name")
    for suffix in ("-wal", "-shm", "-journal"):
        if prod is not None and shadow == Path(str(prod) + suffix):
            raise ShadowPathError("shadow path is a production sidecar file")
    if shadow.exists():
        if shadow.is_symlink():
            raise ShadowPathError("shadow path must not be a symlink")
        conn = sqlite3.connect(shadow)
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "permits" in tables or "companies" in tables:
                raise ShadowPathError("refusing a database that looks like production")
            if tables and "shadow_meta" not in tables:
                raise ShadowPathError("existing file is not an identity-shadow database")
            if "shadow_meta" in tables:
                row = conn.execute("SELECT purpose FROM shadow_meta WHERE singleton=1").fetchone()
                if not row or row[0] != SHADOW_PURPOSE:
                    raise ShadowPathError("existing file is not an identity-shadow database")
        finally:
            conn.close()
    return shadow


def open_shadow(shadow_path, production_path) -> sqlite3.Connection:
    """Validate, then create/open the shadow database and ensure its schema."""
    shadow = validate_shadow_path(shadow_path, production_path)
    shadow.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(shadow, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SHADOW_SCHEMA)
    if conn.execute("SELECT 1 FROM shadow_meta WHERE singleton=1").fetchone() is None:
        conn.execute("INSERT INTO shadow_meta (singleton, purpose, created_at) VALUES (1, ?, ?)",
                     (SHADOW_PURPOSE, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        conn.commit()
    return conn
