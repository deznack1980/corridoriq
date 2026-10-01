"""Persist shadow plumbing_supply relevance. Does not touch opportunity_score."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from pipeline.config.settings import (
    CUSTOMER_RELEVANCE_MODEL_VERSION,
    CUSTOMER_RELEVANCE_PROFILE,
)
from pipeline.db.database import now_iso
from pipeline.relevance.profiles import PROFILES, PROFILE_PLUMBING_SUPPLY
from pipeline.relevance.score import score_project

_BATCH = 500


def seed_relevance_profiles(conn: sqlite3.Connection) -> None:
    now = now_iso()
    for key, label, enabled, notes in PROFILES:
        conn.execute(
            """
            INSERT INTO customer_relevance_profiles
                (profile_key, internal_label, is_enabled, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(profile_key) DO UPDATE SET
                internal_label=excluded.internal_label,
                is_enabled=excluded.is_enabled,
                notes=excluded.notes,
                updated_at=excluded.updated_at
            """,
            (key, label, enabled, notes, now, now),
        )
    conn.commit()


def _load_capabilities(conn: sqlite3.Connection) -> dict[int, list[dict]]:
    by: dict[int, list[dict]] = defaultdict(list)
    rows = conn.execute(
        """
        SELECT company_id, capability, confidence, attribution_role, capability_class
        FROM company_capabilities
        """
    )
    for row in rows:
        by[int(row["company_id"])].append(dict(row))
    return by


def score_project_relevance(
    conn: sqlite3.Connection,
    *,
    profile_key: str | None = None,
    model_version: str | None = None,
) -> dict:
    """Rebuild shadow relevance for one profile. Leaves opportunity_score alone."""
    profile_key = profile_key or CUSTOMER_RELEVANCE_PROFILE
    model_version = model_version or CUSTOMER_RELEVANCE_MODEL_VERSION
    if profile_key != PROFILE_PLUMBING_SUPPLY:
        raise ValueError(f"only {PROFILE_PLUMBING_SUPPLY} is enabled")

    seed_relevance_profiles(conn)
    now = now_iso()
    caps = _load_capabilities(conn)

    conn.execute(
        "DELETE FROM project_customer_relevance WHERE profile_key=?",
        (profile_key,),
    )

    projects = conn.execute(
        """
        SELECT pr.id AS project_id, pr.contractor_company_id, pr.project_category,
               pr.estimated_material_value, pr.project_lifecycle,
               p.permit_type, p.description, p.project_description
        FROM projects pr
        JOIN permits p ON p.id = pr.permit_id
        """
    )

    inserted = 0
    batch: list[tuple] = []
    opportunity_touched = 0
    for row in projects:
        company_id = row["contractor_company_id"]
        result = score_project(
            permit_type=row["permit_type"],
            description=row["description"],
            project_category=row["project_category"],
            extra_text=row["project_description"],
            capabilities=caps.get(int(company_id), []) if company_id else [],
            estimated_material_value=row["estimated_material_value"],
            project_lifecycle=row["project_lifecycle"],
        )
        batch.append(
            (
                row["project_id"],
                company_id,
                profile_key,
                result["relevance_score"],
                result["demand_score"],
                result["contractor_fit_score"],
                result["catalog_scale_score"],
                result["timing_score"],
                json.dumps(result["demand_flags"]),
                result["contractor_fit_basis"],
                result["attribution_role"],
                result["capability_class"],
                model_version,
                now,
                now,
            )
        )
        if len(batch) >= _BATCH:
            _flush(conn, batch)
            inserted += len(batch)
            batch.clear()

    if batch:
        _flush(conn, batch)
        inserted += len(batch)
    conn.commit()

    unchanged = conn.execute(
        "SELECT COUNT(*) n FROM projects WHERE opportunity_score IS NOT NULL"
    ).fetchone()["n"]
    return {
        "profile_key": profile_key,
        "model_version": model_version,
        "rows": inserted,
        "projects_with_opportunity_score": unchanged,
        "opportunity_rows_written": opportunity_touched,
    }


def _flush(conn: sqlite3.Connection, batch: list[tuple]) -> None:
    conn.executemany(
        """
        INSERT INTO project_customer_relevance (
            project_id, company_id, profile_key, relevance_score,
            demand_score, contractor_fit_score, catalog_scale_score, timing_score,
            demand_flags, contractor_fit_basis, attribution_role, capability_class,
            model_version, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        batch,
    )
