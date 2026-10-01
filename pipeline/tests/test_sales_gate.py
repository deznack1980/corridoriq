"""Phase 4E sales-readiness gate. Ranking tables stay untouched."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone

from pipeline.company_resolution.merge import merge_companies
from pipeline.config.settings import SCHEMA_PATH
from pipeline.contactability.official import apply_official_public_contacts
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.crm import serializers
from pipeline.crm import service as crm_service
from pipeline.relevance.classify import seed_relevance_profiles
from pipeline.roc.normalize import normalize_company_name
from pipeline.sales_gate.audit import audit_account, write_account_reviews
from pipeline.sales_gate.evidence import sales_why_now
from pipeline.sales_gate.identity import HIGH, write_identity_reviews
from pipeline.sales_gate.persons import PERMIT_APPLICANT, classify_person_record, review_person_accounts


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
    seed_relevance_profiles(c)
    return c


def _company(c, name, **fields):
    now = _now()
    cid = fields.get("id")
    cols = """
        normalized_name, display_name, legal_name, city, postal_code,
        lifecycle_state, created_at, updated_at
    """
    vals = (
        normalize_company_name(name),
        name,
        name,
        fields.get("city"),
        fields.get("postal_code"),
        now,
        now,
    )
    if cid is None:
        cur = c.execute(
            f"INSERT INTO companies ({cols}) VALUES (?,?,?,?,?, 'active', ?, ?)",
            vals,
        )
        c.commit()
        return cur.lastrowid
    c.execute(
        f"INSERT INTO companies (id, {cols}) VALUES (?, ?,?,?,?,?, 'active', ?, ?)",
        (cid, *vals),
    )
    c.commit()
    return cid


def _priority(c, company_id, identity="fuel_gas_specialist", score=90.0, **extra):
    now = _now()
    c.execute(
        """
        INSERT INTO company_customer_priority (
            profile_key, company_id, account_priority_score, trade_identity,
            trade_identity_score, activity_score, recency_score,
            demand_quality_score, project_quality_score, confidence_score,
            relevant_30d, relevant_90d, relevant_180d, fuel_gas_project_count,
            plumbing_project_count, active_relevant_project_count,
            identity_basis, why_now, primary_demand_category, demand_categories,
            model_version, generated_at
        ) VALUES ('plumbing_supply', ?, ?, ?, 80, 50, 60, 55, 50, 70,
                  ?, ?, ?, ?, ?, ?,
                  'permit_behavior', 'fuel gas specialist; 8 relevant jobs in 30d',
                  ?, ?, 'account-priority-v1', ?)
        """,
        (
            company_id,
            score,
            identity,
            extra.get("n30", 8),
            extra.get("n90", 19),
            extra.get("n180", 31),
            extra.get("n_gas", 126),
            extra.get("n_plum", 0),
            extra.get("n_rel", 232),
            extra.get("primary", "fuel_gas"),
            extra.get("demand", '{"fuel_gas": 126}'),
            now,
        ),
    )
    c.commit()


def _permit(c, company_id, desc, permit_number="P1"):
    now = _now()
    c.execute(
        """
        INSERT INTO permits (jurisdiction, permit_number, permit_type, status,
            description, project_description, issued_date, contractor_company_id,
            first_seen_at, last_updated_at)
        VALUES ('phoenix_az', ?, 'GAS', 'ISSUED', ?, ?, '2026-09-15', ?, ?, ?)
        """,
        (permit_number, desc, desc, company_id, now, now),
    )
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """
        INSERT INTO projects (permit_id, jurisdiction, project_category,
            opportunity_score, opportunity_date, analyzed_at)
        VALUES (?, 'phoenix_az', 'Other', 74.2, '2026-09-15', ?)
        """,
        (pid, now),
    )
    prid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """
        INSERT INTO project_customer_relevance (
            project_id, company_id, profile_key, relevance_score, demand_score,
            contractor_fit_score, catalog_scale_score, timing_score,
            model_version, created_at, updated_at
        ) VALUES (?, ?, 'plumbing_supply', 79.8, 80, 80, 70, 70, 'relevance-plumbing-v1', ?, ?)
        """,
        (prid, company_id, now, now),
    )
    c.commit()
    return prid


def test_why_now_is_evidence_based_not_generic():
    text = sales_why_now(
        display_name="Arizona Propane",
        trade_identity="fuel_gas_specialist",
        n30=8,
        n90=19,
        n180=31,
        n_rel=232,
        n_gas=126,
        n_plum=0,
        counts={"fuel_gas": 126},
        strongest_desc="INSTALL 500 GAL U/G PROPANE TANK",
        strongest_date="2026-09-15",
        roc_class="R-37R; CR-5",
        identity_status="HIGH_CONFIDENCE",
    )
    low = text.lower()
    assert "high-value plumbing prospect" not in low
    assert "strong opportunity" not in low
    assert "8 relevant" in low
    assert "fuel gas" in low
    assert "126 historical fuel-gas" in low
    assert "propane tank" in low


def test_identity_review_does_not_mutate_roc_matches():
    c = conn()
    cid = _company(c, "Arizona Propane", id=40676, city="Scottsdale")
    now = _now()
    c.execute(
        """
        INSERT INTO roc_company_matches (
            company_id, roc_license_id, match_status, match_confidence,
            match_reasons, matching_version, created_at, updated_at
        ) VALUES (?, NULL, 'POSSIBLE_MATCH', 72, '["name_to_dba"]', 'roc-match-v1', ?, ?)
        """,
        (cid, now, now),
    )
    c.commit()
    stats = write_identity_reviews(c)
    assert stats["applied_to_roc_matches"] == 0
    status = c.execute(
        "SELECT match_status FROM roc_company_matches WHERE company_id=?", (cid,)
    ).fetchone()
    assert status["match_status"] == "POSSIBLE_MATCH"
    rec = c.execute(
        "SELECT recommended_identity_status FROM sales_identity_reviews WHERE company_id=?",
        (cid,),
    ).fetchone()
    assert rec["recommended_identity_status"] == HIGH
    assert c.execute(
        "SELECT COUNT(*) n FROM companies WHERE lifecycle_state='merged'"
    ).fetchone()["n"] == 0


def test_person_pool_permit_is_not_sales_ready():
    c = conn()
    cid = _company(c, "Kris Espinoza")
    _permit(
        c,
        cid,
        "CONSTRUCT A 338-SF SWIMMING POOL AND A 49-SF SPA WITH NATURAL GAS HEAT. "
        "INSTALLATION OF 61-FT OF 2-IN PE GAS LINE BURIED 18-IN DEEP",
        "PMT26-15877",
    )
    rec = classify_person_record(c, cid, "Kris Espinoza")
    assert rec["person_class"] == PERMIT_APPLICANT
    assert rec["sales_readiness"] == "NOT_SALES_READY"
    _priority(c, cid, identity="recurring_fuel_gas", score=77.4, n30=1, n90=1)
    review_person_accounts(c, limit=10)
    stored = c.execute(
        "SELECT person_class, sales_readiness FROM sales_person_reviews WHERE company_id=?",
        (cid,),
    ).fetchone()
    assert stored["person_class"] == PERMIT_APPLICANT
    assert stored["sales_readiness"] == "NOT_SALES_READY"


def test_official_umbrella_and_audit_guardrails():
    c = conn()
    umbrella = _company(c, "Umbrella Plumbing LLC")
    az = _company(c, "Arizona Propane", id=40676)
    _priority(c, az)
    _permit(c, az, "INSTALL 500 GAL U/G PROPANE TANK", "2603800")
    before_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    before_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    before_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority"))
    before_fk = list(c.execute("SELECT id, contractor_company_id FROM permits"))
    crm_n = c.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"]
    official = apply_official_public_contacts(c, [umbrella, az])
    assert official["companies_matched"] >= 1
    phone = c.execute(
        "SELECT contact_value, verification_status, source_family FROM company_contact_channels "
        "WHERE company_id=? AND contact_type='business_phone'",
        (umbrella,),
    ).fetchone()
    assert phone["contact_value"] == "480-869-6952"
    assert phone["verification_status"] == "VERIFIED"
    assert phone["source_family"] == "official_website"
    write_identity_reviews(c)
    rows = write_account_reviews(c, limit=25)
    assert rows
    rec = rows[0]
    assert rec["sales_readiness"] == "SALES_READY"
    assert rec["account_segment"] == "FUEL_GAS_PROPANE"
    assert "high-value" not in rec["sales_why_now"].lower()
    after_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    after_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    after_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority"))
    after_fk = list(c.execute("SELECT id, contractor_company_id FROM permits"))
    assert before_opp == after_opp
    assert before_rel == after_rel
    assert before_pri == after_pri
    assert after_fk == before_fk
    assert c.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"] == crm_n
    roc_on = c.execute(
        "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='roc'"
    ).fetchone()
    assert int(roc_on[0]) == 0
    company = serializers.serialize_company(
        {"id": az, "display_name": "Arizona Propane", "sales_readiness": "SALES_READY"}
    )
    assert "sales_readiness" not in company
    src = inspect.getsource(crm_service.opportunities)
    assert "sales_account_reviews" not in src
    assert "account_priority_score" not in src
    assert callable(merge_companies)
    assert c.execute(
        "SELECT COUNT(*) n FROM companies WHERE lifecycle_state='merged'"
    ).fetchone()["n"] == 0


def test_audit_does_not_require_overlay_for_unknown_company():
    c = conn()
    cid = _company(c, "Some Plumber LLC")
    _priority(
        c,
        cid,
        identity="plumbing_specialist",
        score=71.0,
        primary="plumbing_service",
        demand='{"plumbing_service": 4}',
    )
    rec = audit_account(
        c,
        {
            "company_id": cid,
            "display_name": "Some Plumber LLC",
            "account_priority_score": 71.0,
            "trade_identity": "plumbing_specialist",
            "relevant_30d": 1,
            "relevant_90d": 2,
            "relevant_180d": 2,
            "active_relevant_project_count": 4,
            "fuel_gas_project_count": 0,
            "plumbing_project_count": 4,
            "demand_categories": '{"plumbing_service": 4}',
            "primary_demand_category": "plumbing_service",
        },
    )
    assert rec["sales_readiness"] in {
        "SALES_READY",
        "SALES_READY_WITH_CAUTION",
        "RESEARCH_FIRST",
        "NOT_SALES_READY",
    }
    assert rec["account_segment"]
