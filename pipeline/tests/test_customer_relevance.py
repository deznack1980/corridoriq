"""Shadow plumbing_supply customer relevance. Must not change opportunity_score."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone

from pipeline.config.settings import SCHEMA_PATH
from pipeline.contractor_intel.classify import classify_companies
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.crm import service as crm_service
from pipeline.relevance.classify import score_project_relevance
from pipeline.relevance.demand import demand_score
from pipeline.relevance.score import INCIDENTAL_RELEVANCE_CAP, score_project


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute(
        "INSERT INTO jurisdictions (slug, name, state, status) "
        "VALUES ('phoenix_az','Phoenix','AZ','connected')"
    )
    seed_enrichment_registry(c)
    return c


def _company(c, name, *, license_number=None):
    now = _now()
    cur = c.execute(
        """
        INSERT INTO companies (normalized_name, display_name, legal_name,
            license_number, lifecycle_state, created_at, updated_at)
        VALUES (?,?,?,?, 'active', ?, ?)
        """,
        (name.upper(), name, name, license_number, now, now),
    )
    c.commit()
    return cur.lastrowid


def _permit(c, company_id, *, num, permit_type="Building", description="",
            category="Other", score=70.0, issued="2026-08-01",
            lifecycle="Permit Issued", material_value=None):
    now = _now()
    cur = c.execute(
        """
        INSERT INTO permits (jurisdiction, permit_number, permit_type, status,
            description, issued_date, contractor_company_id, first_seen_at, last_updated_at)
        VALUES ('phoenix_az', ?, ?, 'issued', ?, ?, ?, ?, ?)
        """,
        (num, permit_type, description, issued, company_id, now, now),
    )
    permit_id = cur.lastrowid
    c.execute(
        """
        INSERT INTO projects (permit_id, jurisdiction, contractor_company_id,
            project_category, opportunity_score, opportunity_date, analysis_version,
            analyzed_at, project_lifecycle, estimated_material_value)
        VALUES (?, 'phoenix_az', ?, ?, ?, ?, 'test', ?, ?, ?)
        """,
        (permit_id, company_id, category, score, issued, now, lifecycle, material_value),
    )
    c.commit()
    return permit_id


def _rel(c, company_id):
    return c.execute(
        """
        SELECT r.relevance_score, r.demand_score, r.contractor_fit_score,
               r.contractor_fit_basis, r.capability_class, pr.opportunity_score
        FROM project_customer_relevance r
        JOIN projects pr ON pr.id = r.project_id
        WHERE r.company_id=?
        ORDER BY r.relevance_score DESC
        """,
        (company_id,),
    ).fetchone()


def test_racking_demand_is_near_zero():
    score, flags = demand_score(
        permit_type="COM",
        description="High piled combustible storage racking and forklift aisles",
        project_category="Warehouse",
    )
    assert score < 15
    assert "racking_or_material_handling" in flags


def test_plumbing_permit_demand_is_high():
    score, flags = demand_score(
        permit_type="Plumbing",
        description="Water heater replacement and re-pipe",
        project_category="Plumbing",
    )
    assert score >= 80
    assert "plumbing_permit_or_category" in flags


def test_toyotalift_style_low_relevance_score_unchanged():
    c = conn()
    cid = _company(c, "TOYOTALIFT OF ARIZONA INC")
    _permit(
        c, cid, num="TL1", permit_type="COM",
        description="Install high piled storage racking and mezzanine in existing facility",
        category="Warehouse", score=62.5,
    )
    classify_companies(c)
    before = c.execute("SELECT opportunity_score FROM projects").fetchone()[0]
    stats = score_project_relevance(c)
    after = c.execute("SELECT opportunity_score FROM projects").fetchone()[0]
    row = _rel(c, cid)
    assert before == 62.5
    assert after == 62.5
    assert stats["opportunity_rows_written"] == 0
    assert row["relevance_score"] < 30
    assert row["demand_score"] < 20


def test_plumbing_specialist_high_relevance():
    c = conn()
    cid = _company(c, "Kerns Plumbing LLC")
    for i in range(6):
        _permit(
            c, cid, num=f"KP{i}", permit_type="Plumbing",
            description="Water heater replacement, re-pipe, and backflow preventer",
            category="Plumbing", score=48.0,
        )
    classify_companies(c)
    score_project_relevance(c)
    row = _rel(c, cid)
    assert row["opportunity_score"] == 48.0
    assert row["relevance_score"] >= 70
    assert row["capability_class"] in {"specialist_capability", "recurring_trade_capability"}


def test_gc_with_plumbing_scope_not_excluded():
    c = conn()
    cid = _company(c, "Desert Custom Builders Inc")
    _permit(
        c, cid, num="GC1", permit_type="COM",
        description="Restaurant TI kitchen plumbing, grease interceptor, and gas line to appliances",
        category="Restaurant", score=80.0,
    )
    classify_companies(c)
    score_project_relevance(c)
    row = _rel(c, cid)
    assert row["relevance_score"] >= 35
    assert row["relevance_score"] < 80
    assert "excluded" not in (row["contractor_fit_basis"] or "")


def test_incidental_cannot_reach_high_band():
    result = score_project(
        permit_type="Plumbing",
        description="Water heater replacement",
        project_category="Plumbing",
        capabilities=[{
            "capability": "plumbing",
            "confidence": 55,
            "attribution_role": "gc_of_record",
            "capability_class": "incidental_project_scope",
        }],
        estimated_material_value=250000,
        project_lifecycle="Permit Issued",
    )
    assert result["relevance_score"] <= INCIDENTAL_RELEVANCE_CAP
    assert result["relevance_score"] < 70


def test_relevance_idempotent_and_does_not_write_opportunity():
    c = conn()
    cid = _company(c, "Valley Pipe Co")
    _permit(c, cid, num="VP1", permit_type="Plumbing",
            description="Sewer line repair", category="Plumbing", score=41.0)
    classify_companies(c)
    a = score_project_relevance(c)
    b = score_project_relevance(c)
    n = c.execute("SELECT COUNT(*) n FROM project_customer_relevance").fetchone()["n"]
    assert a["rows"] == b["rows"] == n == 1
    assert c.execute("SELECT opportunity_score FROM projects").fetchone()[0] == 41.0


def test_schema_and_disabled_profiles():
    c = conn()
    score_project_relevance(c)
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "customer_relevance_profiles" in names
    assert "project_customer_relevance" in names
    enabled = {
        r["profile_key"]: r["is_enabled"]
        for r in c.execute("SELECT profile_key, is_enabled FROM customer_relevance_profiles")
    }
    assert enabled["plumbing_supply"] == 1
    assert enabled["electrical_distributor"] == 0
    assert enabled["roofing_supplier"] == 0


def test_dashboard_query_still_orders_by_opportunity_score():
    src = inspect.getsource(crm_service.opportunities)
    assert "relevance_score" not in src
    assert "ORDER BY COALESCE(pr.opportunity_score,0) DESC" in src
