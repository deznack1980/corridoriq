"""TenantContext: proof that a caller resolved an active tenant through the
registry. Tenant-owned storage accepts nothing else — there is no default
tenant and no way to pass a bare tenant ID to a data-access function."""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline.tenancy.model import Tenant, TenantError

_ISSUER = object()


@dataclass(frozen=True)
class TenantContext:
    tenant: Tenant
    _issuer: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._issuer is not _ISSUER:
            raise TenantError("tenant context must be issued by TenantRegistry.context()")
        if not isinstance(self.tenant, Tenant):
            raise TenantError("tenant context requires a Tenant")
        if not self.tenant.active:
            raise TenantError("tenant is inactive")

    @property
    def tenant_id(self) -> str:
        return self.tenant.tenant_id


def issue_context(tenant: Tenant) -> TenantContext:
    """Registry-only constructor."""
    return TenantContext(tenant, _ISSUER)


def require_context(ctx) -> TenantContext:
    """Fail closed when no (or a forged) tenant context is supplied."""
    if not isinstance(ctx, TenantContext):
        raise TenantError("tenant context is required")
    return ctx
