"""One-time CorridorIQ owner/admin bootstrap.

Run only with CORRIDORIQ_BOOTSTRAP_ADMIN_EMAIL and
CORRIDORIQ_BOOTSTRAP_ADMIN_PASSWORD set. The password is hashed before storage
and the account is forced to change it on first login.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from pipeline.auth import passwords, sessions
from pipeline.auth.seed import ensure_default_organization
from pipeline.auth.service import create_user, normalize_email, write_audit
from pipeline.db.database import init_db


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def main() -> None:
    email = normalize_email(os.environ.get("CORRIDORIQ_BOOTSTRAP_ADMIN_EMAIL", ""))
    password = os.environ.get("CORRIDORIQ_BOOTSTRAP_ADMIN_PASSWORD", "")
    if not email or "@" not in email:
        raise SystemExit("bootstrap admin email is missing or invalid")
    if len(password) < 12:
        raise SystemExit("bootstrap admin password is missing or too short")

    conn = init_db()
    try:
        org_id = ensure_default_organization(conn)
        role = conn.execute("SELECT id FROM roles WHERE name='admin'").fetchone()
        if role is None:
            raise SystemExit("admin role is not seeded")

        user = conn.execute(
            "SELECT * FROM users WHERE normalized_email=?", (email,)
        ).fetchone()
        if user is None:
            user_id = create_user(
                conn,
                organization_id=org_id,
                email=email,
                password=password,
                display_name="CorridorIQ Owner",
                role_names=["admin"],
                must_change_password=True,
            )
            action = "create_owner_admin"
        else:
            user_id = user["id"]
            conn.execute(
                "UPDATE users SET organization_id=?, password_hash=?, is_active=1, "
                "must_change_password=1, failed_login_count=0, locked_until=NULL, "
                "updated_at=? WHERE id=?",
                (org_id, passwords.hash_password(password), _now(), user_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO user_roles "
                "(user_id, role_id, assigned_by, assigned_at) VALUES (?,?,NULL,?)",
                (user_id, role["id"], _now()),
            )
            sessions.revoke_user_sessions(conn, user_id, commit=False)
            write_audit(
                conn,
                event_type="admin_change",
                success=True,
                organization_id=org_id,
                resource_type="user",
                resource_id=user_id,
                action="bootstrap_owner_admin",
                details={"email": email, "roles": ["admin"]},
                commit=False,
            )
            conn.commit()
            action = "reset_and_promote_owner_admin"

        print(f"CorridorIQ owner/admin bootstrap complete ({action}).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
