"""CLI: grant or revoke the business-owner role (CEO Agent access).

    python -m pipeline.auth.grant_owner --email owner@example.com --dry-run
    python -m pipeline.auth.grant_owner --email owner@example.com
    python -m pipeline.auth.grant_owner --email owner@example.com --revoke

The owner role carries owner.ceo_agent, which admin.system never implies. It
is assigned only here, on the server, never through the admin API, so an
administrator cannot grant it to themselves. The target must be an active
administrator of CorridorIQ's own organization. Every change is audited.
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

from pipeline.auth.rbac import OWNER_ROLE
from pipeline.auth.seed import DEFAULT_ORG_SLUG
from pipeline.auth.service import normalize_email, write_audit


class OwnerGrantError(Exception):
    """The requested grant or revoke is not allowed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _target(conn: sqlite3.Connection, email: str):
    norm = normalize_email(email)
    row = conn.execute(
        "SELECT u.id, u.email, u.is_active, o.slug AS org_slug FROM users u "
        "JOIN organizations o ON o.id = u.organization_id WHERE u.normalized_email=?",
        (norm,),
    ).fetchone()
    if row is None:
        raise OwnerGrantError("No user has that email.")
    return row


def _roles(conn: sqlite3.Connection, user_id: int) -> set[str]:
    return {r["name"] for r in conn.execute(
        "SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=?",
        (user_id,))}


def grant_owner(conn: sqlite3.Connection, email: str, *, dry_run: bool = False) -> dict:
    """Give an existing CorridorIQ administrator the owner role. Idempotent."""
    row = _target(conn, email)
    if not row["is_active"]:
        raise OwnerGrantError("That account is disabled.")
    if row["org_slug"] != DEFAULT_ORG_SLUG:
        raise OwnerGrantError("The owner must belong to CorridorIQ's own organization.")
    roles = _roles(conn, row["id"])
    if "admin" not in roles:
        raise OwnerGrantError("The owner role is granted only to an existing administrator.")
    if OWNER_ROLE in roles:
        return {"user_id": row["id"], "changed": False, "roles": sorted(roles)}
    if dry_run:
        return {"user_id": row["id"], "changed": False, "would_change": True,
                "roles": sorted(roles | {OWNER_ROLE})}
    role = conn.execute("SELECT id FROM roles WHERE name=?", (OWNER_ROLE,)).fetchone()
    if role is None:
        raise OwnerGrantError("The owner role is not seeded yet; deploy this release first.")
    role_id = role["id"]
    conn.execute(
        "INSERT OR IGNORE INTO user_roles (user_id, role_id, assigned_by, assigned_at) "
        "VALUES (?,?,NULL,?)", (row["id"], role_id, _now()))
    write_audit(conn, event_type="admin_change", success=True, user_id=None,
                resource_type="user", resource_id=row["id"], action="grant_owner_role",
                details={"role": OWNER_ROLE, "via": "server_cli"}, commit=False)
    conn.commit()
    return {"user_id": row["id"], "changed": True, "roles": sorted(_roles(conn, row["id"]))}


def revoke_owner(conn: sqlite3.Connection, email: str, *, dry_run: bool = False) -> dict:
    row = _target(conn, email)
    roles = _roles(conn, row["id"])
    if OWNER_ROLE not in roles:
        return {"user_id": row["id"], "changed": False, "roles": sorted(roles)}
    if dry_run:
        return {"user_id": row["id"], "changed": False, "would_change": True,
                "roles": sorted(roles - {OWNER_ROLE})}
    conn.execute(
        "DELETE FROM user_roles WHERE user_id=? AND role_id=(SELECT id FROM roles WHERE name=?)",
        (row["id"], OWNER_ROLE))
    write_audit(conn, event_type="admin_change", success=True, user_id=None,
                resource_type="user", resource_id=row["id"], action="revoke_owner_role",
                details={"role": OWNER_ROLE, "via": "server_cli"}, commit=False)
    conn.commit()
    return {"user_id": row["id"], "changed": True, "roles": sorted(_roles(conn, row["id"]))}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Grant or revoke CorridorIQ owner (CEO Agent) access.")
    parser.add_argument("--email", required=True, help="Email of an existing CorridorIQ administrator")
    parser.add_argument("--revoke", action="store_true", help="Remove the owner role instead")
    parser.add_argument("--dry-run", action="store_true", help="Show the result without writing")
    args = parser.parse_args(argv)

    from pipeline.db.database import init_db

    # init_db applies the same idempotent schema + role seed the server runs at startup.
    conn = init_db()
    try:
        fn = revoke_owner if args.revoke else grant_owner
        result = fn(conn, args.email, dry_run=args.dry_run)
    except OwnerGrantError as exc:
        raise SystemExit(f"Error: {exc}")
    finally:
        conn.close()
    verb = "revoke" if args.revoke else "grant"
    if args.dry_run:
        state = "would change" if result.get("would_change") else "no change needed"
        print(f"[dry run] owner {verb} for user #{result['user_id']}: {state}")
    else:
        print(f"Owner {verb} for user #{result['user_id']}: "
              f"{'done' if result['changed'] else 'no change needed'}")
    print(f"  Roles: {', '.join(result['roles'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
