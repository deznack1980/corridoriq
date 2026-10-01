"""The data-trust audit stays read-only."""

from __future__ import annotations

import sqlite3

from agents.ceo.audit.data_trust import SELECTS, run_audit
from agents.ceo.operating_state.collector import connect_readonly


def test_audit_sql_is_select_only():
    for name, sql in SELECTS.items():
        assert sql.lstrip().upper().startswith("SELECT"), name
        assert "INSERT " not in sql.upper()
        assert "UPDATE " not in sql.upper()
        assert "DELETE " not in sql.upper()


def test_audit_does_not_write(tmp_path):
    db = tmp_path / "audit.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE projects (id INTEGER, opportunity_score REAL)")
    conn.execute("INSERT INTO projects VALUES (1, 77)")
    conn.commit()
    conn.close()
    payload = run_audit(db)
    assert payload["database"] == "readonly"
    check = sqlite3.connect(db)
    score = check.execute("SELECT opportunity_score FROM projects").fetchone()[0]
    check.close()
    assert score == 77
    ro = connect_readonly(db)
    ro.close()
