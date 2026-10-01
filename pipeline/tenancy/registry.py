"""Tenant registry and path resolution.

Layout under an explicit root (no implicit default — callers must pass it):

    <root>/registry.db              tenant IDs, display names, type, status
    <root>/tenants/<tenant_id>/     one directory per tenant
        tenant.db                   that tenant's private data

The registry holds no customer data. It lives outside the shared CorridorIQ
intelligence database on purpose.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.tenancy.context import TenantContext, issue_context, require_context
from pipeline.tenancy.model import (
    Tenant,
    TenantError,
    TenantType,
    validate_display_name,
    validate_tenant_id,
)

REGISTRY_FILENAME = "registry.db"
TENANTS_DIRNAME = "tenants"

_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    tenant_id    TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    tenant_type  TEXT NOT NULL CHECK (tenant_type IN ('supplier')),
    active       INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at   TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class TenantRegistry:
    def __init__(self, root):
        if root is None or (isinstance(root, str) and not root.strip()):
            raise TenantError("tenant root is required")
        self.root = Path(root).expanduser().resolve()
        self.tenants_dir = self.root / TENANTS_DIRNAME

    # ---- registry database ------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        self.root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.root / REGISTRY_FILENAME, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.executescript(_REGISTRY_SCHEMA)
        return conn

    @staticmethod
    def _tenant(row) -> Tenant:
        return Tenant(
            tenant_id=row["tenant_id"],
            display_name=row["display_name"],
            tenant_type=TenantType(row["tenant_type"]),
            active=bool(row["active"]),
            created_at=row["created_at"],
        )

    def create(self, tenant_id, display_name, tenant_type=TenantType.SUPPLIER) -> Tenant:
        tenant_id = validate_tenant_id(tenant_id)
        display_name = validate_display_name(display_name)
        if TenantType(tenant_type) is not TenantType.SUPPLIER:
            raise TenantError("only supplier tenants are supported")
        conn = self._connect()
        try:
            try:
                conn.execute(
                    "INSERT INTO tenants (tenant_id, display_name, tenant_type, active, created_at) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (tenant_id, display_name, TenantType.SUPPLIER.value, _now()),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise TenantError("tenant already exists") from exc
        finally:
            conn.close()
        self.tenant_dir(tenant_id).mkdir(parents=True, exist_ok=True)
        return self.get(tenant_id)

    def get(self, tenant_id) -> Tenant:
        tenant_id = validate_tenant_id(tenant_id)
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            raise TenantError("unknown tenant")
        return self._tenant(row)

    def list(self) -> list[Tenant]:
        conn = self._connect()
        try:
            return [self._tenant(r) for r in conn.execute("SELECT * FROM tenants ORDER BY tenant_id")]
        finally:
            conn.close()

    def set_active(self, tenant_id, active: bool) -> Tenant:
        tenant_id = validate_tenant_id(tenant_id)
        conn = self._connect()
        try:
            cur = conn.execute("UPDATE tenants SET active=? WHERE tenant_id=?", (1 if active else 0, tenant_id))
            conn.commit()
        finally:
            conn.close()
        if cur.rowcount != 1:
            raise TenantError("unknown tenant")
        return self.get(tenant_id)

    def context(self, tenant_id) -> TenantContext:
        """The only way to obtain a TenantContext. Unknown or inactive → error."""
        tenant = self.get(tenant_id)
        if not tenant.active:
            raise TenantError("tenant is inactive")
        return issue_context(tenant)

    def confirm(self, ctx) -> TenantContext:
        """Re-check a context against the registry (status may have changed)."""
        ctx = require_context(ctx)
        current = self.get(ctx.tenant_id)
        if not current.active:
            raise TenantError("tenant is inactive")
        return ctx

    # ---- paths ------------------------------------------------------------
    def tenant_dir(self, tenant_id) -> Path:
        """Validated directory for one tenant. Built only from a validated ID;
        the resolved path (symlinks/junctions followed) must sit directly inside
        <root>/tenants, or the call fails."""
        tenant_id = validate_tenant_id(tenant_id)
        base = self.tenants_dir.resolve()
        candidate = (base / tenant_id)
        resolved = candidate.resolve()
        if resolved.parent != base or resolved.name != tenant_id:
            raise TenantError("tenant path escapes the tenant root")
        return resolved

    def store(self, ctx):
        from pipeline.tenancy.store import TenantStore
        return TenantStore(self.confirm(ctx), self)
