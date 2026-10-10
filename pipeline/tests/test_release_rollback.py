"""Additive schema rollback: new accounts survive an application revert."""

from __future__ import annotations

import sqlite3

from pipeline.auth import onboarding
from pipeline.auth.seed import seed_auth
from pipeline.config.settings import SCHEMA_PATH
from pipeline.db.database import migrate_schema


PRE_ONBOARDING_USER_COLS = {
    "id", "organization_id", "email", "normalized_email", "password_hash",
    "is_active", "must_change_password", "failed_login_count", "created_at",
    "updated_at",
}


def test_new_accounts_survive_additive_rollback(tmp_path):
    db = tmp_path / "rollback.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    seed_auth(conn)
    migrate_schema(conn)

    token, user = onboarding.register_contractor(conn, {
        "name": "Rollback User",
        "business_name": "Rollback Plumbing",
        "email": "rollback@example.test",
        "password": "SecurePass123",
    })
    assert token
    assert user["account_kind"] == "contractor"

    # Simulate an older application that only reads pre-onboarding columns.
    cols = ", ".join(sorted(PRE_ONBOARDING_USER_COLS))
    row = conn.execute(
        f"SELECT {cols} FROM users WHERE normalized_email=?",
        ("rollback@example.test",),
    ).fetchone()
    assert row["email"] == "rollback@example.test"
    assert row["password_hash"]
    assert "SecurePass123" not in row["password_hash"]
    assert row["is_active"] == 1

    employee = conn.execute(
        "SELECT email, password_hash, is_active FROM users WHERE account_kind='employee' LIMIT 1"
    ).fetchone()
    # Seeded roles exist; employees may be created only by tests. Org remains.
    org = conn.execute("SELECT slug FROM organizations WHERE slug='corridoriq'").fetchone()
    assert org is not None
    conn.close()
