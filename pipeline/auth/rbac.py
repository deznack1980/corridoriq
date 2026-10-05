"""Role-based access control: permission catalog, role mappings, and checks.

Authorization is enforced in the backend. Every permission decision goes
through these helpers — hiding UI controls is never treated as security.
"""

from __future__ import annotations

import sqlite3

# Full permission catalog (permission_key -> description).
PERMISSIONS: dict[str, str] = {
    "companies.view": "View all companies in the organization",
    "companies.view_assigned": "View only assigned companies",
    "companies.assign": "Assign / reassign companies to representatives",
    "companies.export": "Export company lists",
    "projects.view": "View all projects",
    "projects.view_assigned": "View projects for assigned companies",
    "permits.view": "View permit activity",
    "crm.relationships.view": "View CRM relationship status",
    "crm.relationships.update": "Update CRM relationship status",
    "crm.activities.view": "View CRM activities",
    "crm.activities.create": "Create CRM activities",
    "crm.activities.edit_own": "Edit own CRM activities",
    "crm.activities.edit_all": "Edit any CRM activity",
    "crm.tasks.view": "View CRM tasks",
    "crm.tasks.create": "Create CRM tasks",
    "crm.tasks.edit_own": "Edit own tasks",
    "crm.tasks.assign": "Assign tasks to other users",
    "crm.tasks.complete": "Complete tasks",
    "crm.followups.manage": "Manage follow-ups",
    "reports.view": "View reports",
    "reports.export": "Export reports",
    "products.view": "Search products and view supplier comparisons",
    "products.quote": "Create product quote requests",
    "users.view": "View users",
    "users.create": "Create users",
    "users.update": "Update users",
    "users.disable": "Disable users",
    "roles.manage": "Manage roles and permissions",
    "knowledge.view": "View the Municipal Knowledge Engine",
    "knowledge.review": "Review knowledge queue items",
    "knowledge.approve": "Approve dictionary mappings",
    "supplier_pricing.view": "View restricted supplier pricing",
    "supplier_pricing.manage": "Manage supplier pricing",
    "pipeline.monitor": "View detailed data-pipeline run status and errors",
    "pipeline.run": "Trigger the data pipeline / morning refresh manually",
    "billing.manage": "Start a subscription and manage billing for the organization",
    "admin.system": "Full system administration",
}

# System roles and their granted permissions.
_SALES_REP = {
    "companies.view_assigned", "projects.view_assigned", "permits.view",
    "crm.relationships.view", "crm.relationships.update",
    "crm.activities.view", "crm.activities.create", "crm.activities.edit_own",
    "crm.tasks.view", "crm.tasks.create", "crm.tasks.edit_own",
    "crm.tasks.complete", "crm.followups.manage", "reports.view",
    "products.view", "products.quote",
}
_SALES_MANAGER = _SALES_REP | {
    "companies.view", "companies.assign", "companies.export",
    "projects.view", "crm.activities.edit_all", "crm.tasks.assign",
    "reports.export", "users.view",
    # A supply house's branch/sales manager owns its CorridorIQ subscription.
    "billing.manage",
}
_READ_ONLY = {
    "companies.view_assigned", "projects.view_assigned", "permits.view",
    "crm.relationships.view", "crm.activities.view", "crm.tasks.view",
    "reports.view", "products.view",
}
_FULFILLMENT = {
    # Deliberately NO unrestricted contractor intelligence.
    "crm.tasks.view", "crm.tasks.complete", "reports.view", "products.view",
}
_ESTIMATOR = {
    "projects.view_assigned", "permits.view",
    "crm.tasks.view", "crm.tasks.complete",
    "reports.view", "products.view", "products.quote",
}

# Contractor account owner: manages its own Contractor Pro subscription only.
# Deliberately no supplier-intelligence, CRM or product permissions.
_CONTRACTOR_OWNER = {"billing.manage"}

ROLES: dict[str, dict] = {
    "admin": {
        "display_name": "Administrator",
        "description": "Full system access.",
        "permissions": set(PERMISSIONS.keys()),
    },
    "sales_manager": {
        "display_name": "Sales Manager",
        "description": "Manages team, assignments, and CRM.",
        "permissions": _SALES_MANAGER,
    },
    "sales_representative": {
        "display_name": "Sales Representative",
        "description": "Works assigned companies; read-only intelligence.",
        "permissions": _SALES_REP,
    },
    "estimator": {
        "display_name": "Estimator",
        "description": "Works assigned estimate queue; no org-wide CRM.",
        "permissions": _ESTIMATOR,
    },
    "read_only": {
        "display_name": "Read Only",
        "description": "View-only access to assigned CRM.",
        "permissions": _READ_ONLY,
    },
    "fulfillment_user": {
        "display_name": "Fulfillment User",
        "description": "Fulfillment tasks; no contractor intelligence.",
        "permissions": _FULFILLMENT,
    },
    "contractor_owner": {
        "display_name": "Contractor Account Owner",
        "description": "Contractor account: manages its own Contractor Pro subscription; no supplier intelligence.",
        "permissions": _CONTRACTOR_OWNER,
    },
}

# Post-login landing pages — backend is the source of truth.
LANDING_ADMIN = "admin-dashboard.html"
LANDING_MANAGER = "team-dashboard.html"
LANDING_REP = "sales-dashboard.html"
LANDING_ESTIMATOR = "estimator-work-queue.html"
LANDING_READONLY = "readonly-dashboard.html"
LANDING_CONTRACTOR = "billing.html"


def default_landing_page(roles: list[str] | None, permissions) -> str:
    """Choose the post-login page from roles/permissions (never the UI)."""
    role_set = set(roles or [])
    perms = set(permissions or [])
    if "admin" in role_set or "admin.system" in perms:
        return LANDING_ADMIN
    if "contractor_owner" in role_set:
        return LANDING_CONTRACTOR
    if "sales_manager" in role_set or "companies.assign" in perms:
        return LANDING_MANAGER
    if "estimator" in role_set:
        return LANDING_ESTIMATOR
    if "read_only" in role_set:
        return LANDING_READONLY
    return LANDING_REP


def dashboard_mode(roles: list[str] | None, permissions) -> str:
    """How dashboard data should be scoped for this user."""
    landing = default_landing_page(roles, permissions)
    if landing == LANDING_ADMIN:
        return "organization"
    if landing == LANDING_MANAGER:
        return "team"
    if landing == LANDING_ESTIMATOR:
        return "estimator"
    if landing == LANDING_READONLY:
        return "read_only"
    return "assignment"


def load_user_permissions(conn: sqlite3.Connection, user_id: int) -> set[str]:
    rows = conn.execute(
        """
        SELECT p.permission_key
        FROM user_roles ur
        JOIN role_permissions rp ON rp.role_id = ur.role_id
        JOIN permissions p ON p.id = rp.permission_id
        WHERE ur.user_id = ?
        """,
        (user_id,),
    ).fetchall()
    keys = {r["permission_key"] for r in rows}
    if "admin.system" in keys:
        return set(PERMISSIONS.keys())
    return keys


def load_user_roles(conn: sqlite3.Connection, user_id: int) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id "
        "WHERE ur.user_id=? ORDER BY r.name",
        (user_id,),
    )]


def has_permission(user: dict, permission_key: str) -> bool:
    if not user:
        return False
    perms = user.get("permissions") or set()
    return "admin.system" in perms or permission_key in perms


def can_access_company(conn: sqlite3.Connection, user: dict, company_id: int) -> bool:
    """Record-level access. companies.view -> all org companies;
    companies.view_assigned -> only companies assigned to this user."""
    if has_permission(user, "companies.view"):
        return True
    if not has_permission(user, "companies.view_assigned"):
        return False
    row = conn.execute(
        "SELECT 1 FROM crm_company_relationships "
        "WHERE organization_id=? AND company_id=? AND assigned_user_id=?",
        (user["organization_id"], company_id, user["id"]),
    ).fetchone()
    return row is not None


class AuthzError(Exception):
    """Raised when a permission or record-level check fails."""

    def __init__(self, message: str = "permission denied", status: int = 403):
        super().__init__(message)
        self.status = status


def require_permission(user: dict, permission_key: str) -> None:
    if not has_permission(user, permission_key):
        raise AuthzError(f"missing permission: {permission_key}")
