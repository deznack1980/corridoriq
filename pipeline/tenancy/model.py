"""Tenant identity: the stable ID, type, and status of a tenant.

Tenant IDs are the only thing ever used to build a filesystem path, so they are
validated strictly: lowercase letters, digits and single hyphens, 3–48
characters, never a Windows device name. Display names are free text and are
never used in paths.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

TENANT_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,47}$")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(10)}
    | {f"lpt{i}" for i in range(10)}
)
_RESERVED = frozenset({"registry", "tenants", "tmp", "temp", "default", "all", "shared"})
DISPLAY_NAME_MAX = 200


class TenantError(Exception):
    """Any tenancy violation. Callers must treat it as fail-closed."""


class TenantType(str, Enum):
    """Supported tenant types. Contractor tenants are intentionally absent."""

    SUPPLIER = "supplier"


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    display_name: str
    tenant_type: TenantType
    active: bool
    created_at: str


def validate_tenant_id(tenant_id) -> str:
    """Return the tenant ID unchanged, or raise TenantError. Never normalizes:
    an ID that needs fixing is rejected rather than reinterpreted."""
    if not isinstance(tenant_id, str) or not tenant_id:
        raise TenantError("tenant ID is required")
    if not TENANT_ID_RE.fullmatch(tenant_id):
        raise TenantError("invalid tenant ID")
    if tenant_id in _WINDOWS_DEVICE_NAMES or tenant_id in _RESERVED:
        raise TenantError("reserved tenant ID")
    return tenant_id


def validate_display_name(name) -> str:
    if not isinstance(name, str) or not name.strip():
        raise TenantError("display name is required")
    name = " ".join(name.split())
    if len(name) > DISPLAY_NAME_MAX:
        raise TenantError("display name is too long")
    return name
