"""Contractor tenants: a parallel registry beside the supplier registry.

Contractor tenancy deliberately does not reuse ``TenantType`` or
``TenantRegistry``: supplier tenancy stays supplier-only, and the two kinds of
tenant can never be confused for each other. A ``ContractorContext`` can only
be issued by ``ContractorRegistry`` and is the only key that opens a
``ContractorStore``; a supplier ``TenantContext`` cannot open one (and vice
versa). Validation of IDs and display names is shared with supplier tenancy.

Layout under an explicit root (no implicit default):

    <root>/contractor_registry.db           IDs, display names, status
    <root>/contractor_tenants/<tenant_id>/contractor.db
"""

from __future__ import annotations

import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline.tenancy.model import TenantError, validate_display_name, validate_tenant_id

REGISTRY_FILENAME = "contractor_registry.db"
TENANTS_DIRNAME = "contractor_tenants"
STORE_FILENAME = "contractor.db"
STORE_FORMAT_VERSION = 1
TENANT_KIND = "contractor"

_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS contractor_tenants (
    tenant_id    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at   TEXT NOT NULL
);
"""

_META_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenant_meta (
    singleton      INTEGER PRIMARY KEY CHECK (singleton = 1),
    tenant_id      TEXT NOT NULL,
    tenant_type    TEXT NOT NULL,
    format_version INTEGER NOT NULL,
    created_at     TEXT NOT NULL
);
"""

_ISSUER = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_contractor_tenant_id() -> str:
    """Stable, non-guessable ID ('c-' + 12 lowercase base32 characters)."""
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    return "c-" + "".join(secrets.choice(alphabet) for _ in range(12))


@dataclass(frozen=True)
class ContractorTenant:
    tenant_id: str
    display_name: str
    active: bool
    created_at: str


@dataclass(frozen=True)
class ContractorContext:
    tenant: ContractorTenant
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER:
            raise TenantError("contractor context must be issued by ContractorRegistry.context()")
        if not isinstance(self.tenant, ContractorTenant):
            raise TenantError("contractor context requires a ContractorTenant")
        if not self.tenant.active:
            raise TenantError("tenant is inactive")

    @property
    def tenant_id(self) -> str:
        return self.tenant.tenant_id


def require_contractor_context(ctx) -> ContractorContext:
    if not isinstance(ctx, ContractorContext):
        raise TenantError("contractor context is required")
    return ctx


class ContractorRegistry:
    def __init__(self, root):
        if root is None or (isinstance(root, str) and not root.strip()):
            raise TenantError("contractor tenant root is required")
        self.root = Path(root).expanduser().resolve()
        self.tenants_dir = self.root / TENANTS_DIRNAME

    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / REGISTRY_FILENAME, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript(_REGISTRY_SCHEMA)
        return conn

    @staticmethod
    def _tenant(row) -> ContractorTenant:
        return ContractorTenant(row["tenant_id"], row["display_name"], bool(row["active"]), row["created_at"])

    def create(self, display_name, tenant_id=None) -> ContractorTenant:
        tenant_id = validate_tenant_id(tenant_id or new_contractor_tenant_id())
        display_name = validate_display_name(display_name)
        conn = self._connect()
        try:
            try:
                conn.execute("INSERT INTO contractor_tenants (tenant_id, display_name, active, created_at) "
                             "VALUES (?, ?, 1, ?)", (tenant_id, display_name, _now()))
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise TenantError("tenant already exists") from exc
        finally:
            conn.close()
        self.tenant_dir(tenant_id).mkdir(parents=True, exist_ok=True)
        return self.get(tenant_id)

    def get(self, tenant_id) -> ContractorTenant:
        tenant_id = validate_tenant_id(tenant_id)
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM contractor_tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise TenantError("unknown tenant")
        return self._tenant(row)

    def rename(self, tenant_id, display_name) -> ContractorTenant:
        tenant_id = validate_tenant_id(tenant_id)
        display_name = validate_display_name(display_name)
        conn = self._connect()
        try:
            cur = conn.execute("UPDATE contractor_tenants SET display_name=? WHERE tenant_id=?",
                               (display_name, tenant_id))
            conn.commit()
        finally:
            conn.close()
        if cur.rowcount != 1:
            raise TenantError("unknown tenant")
        return self.get(tenant_id)

    def set_active(self, tenant_id, active: bool) -> ContractorTenant:
        tenant_id = validate_tenant_id(tenant_id)
        conn = self._connect()
        try:
            cur = conn.execute("UPDATE contractor_tenants SET active=? WHERE tenant_id=?",
                               (1 if active else 0, tenant_id))
            conn.commit()
        finally:
            conn.close()
        if cur.rowcount != 1:
            raise TenantError("unknown tenant")
        return self.get(tenant_id)

    def context(self, tenant_id) -> ContractorContext:
        tenant = self.get(tenant_id)
        if not tenant.active:
            raise TenantError("tenant is inactive")
        return ContractorContext(tenant, _ISSUER)

    def confirm(self, ctx) -> ContractorContext:
        ctx = require_contractor_context(ctx)
        if not self.get(ctx.tenant_id).active:
            raise TenantError("tenant is inactive")
        return ctx

    def tenant_dir(self, tenant_id) -> Path:
        tenant_id = validate_tenant_id(tenant_id)
        base = self.tenants_dir.resolve()
        resolved = (base / tenant_id).resolve()
        if resolved.parent != base or resolved.name != tenant_id:
            raise TenantError("tenant path escapes the tenant root")
        return resolved

    def store(self, ctx) -> "ContractorStore":
        return ContractorStore(self.confirm(ctx), self)


class ContractorStore:
    """The only gateway to one contractor's private SQLite file. Bound to one
    ContractorContext; exposes no API that accepts another tenant ID or path.
    The file carries an owner stamp; a file owned by anyone else (or by a
    supplier tenant) fails closed."""

    def __init__(self, ctx, registry: ContractorRegistry):
        self._ctx = require_contractor_context(ctx)
        self._registry = registry
        self._dir = registry.tenant_dir(self._ctx.tenant_id)
        self._path = self._dir / STORE_FILENAME

    @property
    def tenant_id(self) -> str:
        return self._ctx.tenant_id

    def _open(self) -> sqlite3.Connection:
        self._registry.confirm(self._ctx)
        self._dir.mkdir(parents=True, exist_ok=True)
        if self._path.exists() and self._path.is_symlink():
            raise TenantError("tenant store must not be a symlink")
        conn = sqlite3.connect(self._path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        try:
            conn.executescript(_META_SCHEMA)
            row = conn.execute("SELECT tenant_id, tenant_type FROM tenant_meta WHERE singleton=1").fetchone()
            if row is None:
                conn.execute("INSERT INTO tenant_meta (singleton, tenant_id, tenant_type, format_version, created_at) "
                             "VALUES (1, ?, ?, ?, ?)", (self.tenant_id, TENANT_KIND, STORE_FORMAT_VERSION, _now()))
                conn.commit()
            elif row["tenant_id"] != self.tenant_id or row["tenant_type"] != TENANT_KIND:
                raise TenantError("tenant store belongs to a different tenant")
        except Exception:
            conn.close()
            raise
        return conn

    @contextmanager
    def connect(self):
        conn = self._open()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
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
