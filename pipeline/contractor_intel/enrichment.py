"""Internal enrichment registry.

Future ROC / ACC / UCC / contact payloads go in ``company_enrichment``.
Nothing is ingested here. Source family codes are internal identifiers
and must not be serialized to the sales API.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# Internal codes only. Do not use these strings in customer-facing copy.
RESERVED_SOURCES = (
    ("trade_activity", "Permit/project trade activity (internal)", 1,
     "Live classifier input. Not an external registry."),
    ("roc", "State contractor licence registry (internal)", 0,
     "Ingested into roc_* tables for identity validation. Ranking consumption disabled."),
    ("acc", "State entity registry (reserved)", 0,
     "Reserved. Do not ingest in contractor-intel-v2."),
    ("ucc", "Filing registry (reserved)", 0,
     "Reserved. Do not ingest in contractor-intel-v2."),
    ("contact", "Contact enrichment (reserved)", 0,
     "Registry flag stays off. Phase 4D writes company_contact_channels internally; CRM is unchanged."),
    ("geo", "Geographic behaviour (reserved)", 0, "Reserved."),
    ("velocity", "Permit velocity / value trends (reserved)", 0, "Reserved."),
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def seed_enrichment_registry(conn: sqlite3.Connection) -> None:
    now = _utcnow()
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='enrichment_source_registry'"
    ).fetchone()
    if not exists:
        return
    for family, label, enabled, notes in RESERVED_SOURCES:
        conn.execute(
            """
            INSERT INTO enrichment_source_registry (
                source_family, internal_label, is_enabled, notes, created_at, updated_at
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(source_family) DO UPDATE SET
                internal_label=excluded.internal_label,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (family, label, enabled, notes, now, now),
        )
    conn.commit()
