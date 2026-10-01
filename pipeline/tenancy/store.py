"""TenantStore: the only gateway to one tenant's private SQLite file.

A store is bound to exactly one TenantContext at construction. It exposes no
API that accepts another tenant ID or a path, so a caller holding Supplier A's
store has no means to address Supplier B's data. As defense in depth, every
database carries a ``tenant_meta`` row naming its owner; opening a file whose
owner differs from the bound tenant fails closed (catches copied or swapped
files), and tenant-owned tables also carry the tenant ID on every row.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from pipeline.tenancy.context import require_context
from pipeline.tenancy.model import TenantError

STORE_FILENAME = "tenant.db"
STORE_FORMAT_VERSION = 1

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_meta (
    singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
    tenant_id      TEXT NOT NULL,
    tenant_type    TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
"""


class TenantStore:
    def __init__(self, ctx, registry):
        self._ctx = require_context(ctx)
        self._registry = registry
        self._dir = registry.tenant_dir(self._ctx.tenant_id)
        self._path = self._dir / STORE_FILENAME

    @property
    def tenant_id(self) -> str:
        return self._ctx.tenant_id

    @property
    def tenant(self):
        return self._ctx.tenant

    @property
    def directory(self):
        return self._dir

    def _open(self) -> sqlite3.Connection:
        self._registry.confirm(self._ctx)  # status may have changed since binding
        self._dir.mkdir(parents=True, exist_ok=True)
        if self._path.exists() and self._path.is_symlink():
            raise TenantError("tenant store must not be a symlink")
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            conn.executescript(_META_SCHEMA)
            row = conn.execute("SELECT tenant_id FROM tenant_meta WHERE singleton=1").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO tenant_meta (singleton, tenant_id, tenant_type, format_version, created_at) "
                    "VALUES (1, ?, ?, ?, ?)",
                    (self.tenant_id, self._ctx.tenant.tenant_type.value, STORE_FORMAT_VERSION,
                     datetime.now(timezone.utc).isoformat(timespec="seconds")),
                )
                conn.commit()
            elif row["tenant_id"] != self.tenant_id:
                raise TenantError("tenant store belongs to a different tenant")
        except Exception:
            conn.close()
            raise
        return conn

    @contextmanager
    def connect(self):
        """Connection to this tenant's store only. Commits are the caller's job."""
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        """All-or-nothing write: BEGIN IMMEDIATE (serializes concurrent writers),
        commit on success, roll back on any exception."""
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self, sql: str) -> None:
        with self.connect() as conn:
            conn.executescript(sql)
            conn.commit()
