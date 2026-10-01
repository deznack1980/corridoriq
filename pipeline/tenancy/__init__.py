"""Minimal tenancy for supplier-owned private data (customer books).

Shared CorridorIQ intelligence (permits, companies, ROC, trade, contacts) stays
in the shared database. Anything a supplier provides lives in that supplier's
own tenant store and is reachable only through a TenantContext issued by the
TenantRegistry. There is no default tenant; missing context fails closed.
"""

from pipeline.tenancy.context import TenantContext, require_context
from pipeline.tenancy.model import Tenant, TenantError, TenantType, validate_tenant_id
from pipeline.tenancy.registry import TenantRegistry
from pipeline.tenancy.store import TenantStore

__all__ = [
    "Tenant",
    "TenantContext",
    "TenantError",
    "TenantRegistry",
    "TenantStore",
    "TenantType",
    "require_context",
    "validate_tenant_id",
]
