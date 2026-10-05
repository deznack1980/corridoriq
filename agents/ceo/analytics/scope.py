"""Tenant scope is chosen by the caller, never by the model."""

from __future__ import annotations

from dataclasses import dataclass

from agents.ceo.analytics.guard import AnalyticsError


@dataclass(frozen=True)
class AnalyticsScope:
    kind: str
    organization_id: int | None = None

    def require_organization(self) -> int:
        if self.kind != "organization" or self.organization_id is None:
            raise AnalyticsError("tenant unresolved")
        return int(self.organization_id)


def owner_scope() -> AnalyticsScope:
    return AnalyticsScope("owner")


def organization_scope(organization_id: int) -> AnalyticsScope:
    if organization_id is None:
        raise AnalyticsError("tenant unresolved")
    return AnalyticsScope("organization", int(organization_id))
