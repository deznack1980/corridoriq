"""Shadow account-priority model. Must not rewrite opportunity or project relevance."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone

from pipeline.config.settings import SCHEMA_PATH
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.crm import serializers
from pipeline.crm import service as crm_service
from pipeline.relevance.account import (
    recency_score,
    score_company_accounts,
    score_distribution,
    volume_score,
)
from pipeline.relevance.classify import seed_relevance_profiles
from pipeline.relevance.demand_taxonomy import label_demand, primary_category
from pipeline.relevance.identity import infer_trade_identity, looks_like_gc_cm

AS_OF = datetime(2026, 9, 19, tzinfo=timezone.utc)


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


def _company(c, name):
    now = _now()
    cur = c.execute(
        """
        INSERT INTO companies (normalized_name, display_name, legal_name,
            lifecycle_state, created_at, updated_at)
        VALUES (?,?,?, 'active', ?, ?)
        """,
        (name.upper(), name, name, now, now),
    )
    c.commit()
    return cur.lastrowid


def _cap(c, company_id, capability, *, confidence=100.0, role="trade_contractor",
         klass="specialist_capability"):
    now = _now()
    c.execute(
        """
        INSERT INTO company_capabilities (
            company_id, capability, confidence, evidence_count, distinct_permit_count,
            classification_source, model_version, created_at, updated_at,
            attribution_role, capability_class)
        VALUES (?, ?, ?, 5, 5, 'test', 'contractor-intel-v2', ?, ?, ?, ?)
        """,
        (company_id, capability, confidence, now, now, role, klass),
    )
    c.commit()


def _job(c, company_id, *, num, permit_type="PLMB", description="",
         category="Plumbing", relevance=81.6, issued="2026-08-01"):
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
    cur = c.execute(
        """
        INSERT INTO projects (permit_id, jurisdiction, contractor_company_id,
            project_category, opportunity_score, opportunity_date, analysis_version,
            analyzed_at, project_lifecycle)
        VALUES (?, 'phoenix_az', ?, ?, 50.0, ?, 'test', ?, 'Permit Issued')
        """,
        (permit_id, company_id, category, issued, now),
    )
    project_id = cur.lastrowid
    c.execute(
        """
        INSERT INTO project_customer_relevance (
            project_id, company_id, profile_key, relevance_score, demand_score,
            contractor_fit_score, catalog_scale_score, timing_score,
            demand_flags, contractor_fit_basis, attribution_role, capability_class,
            model_version, created_at, updated_at)
        VALUES (?, ?, 'plumbing_supply', ?, 90, 96, 50, 75,
                '[]', 'plumbing:specialist_capability', 'trade_contractor',
                'specialist_capability', 'relevance-plumbing-v1', ?, ?)
        """,
        (project_id, company_id, relevance, now, now),
    )
    c.commit()
    return project_id


def _row(c, company_id):
    return c.execute(
        "SELECT * FROM company_customer_priority WHERE company_id=?",
        (company_id,),
    ).fetchone()


def test_volume_diminishing_returns():
    one = volume_score(1, 1)
    five = volume_score(5, 5)
    fifty = volume_score(50, 50)
    assert five > one
    assert fifty > five
    assert fifty < five * 4
    assert fifty < 100.0


def test_stale_project_recency_suppressed():
    stale = recency_score(
        n30=0, n90=0, n180=0, n365=0,
        most_recent=datetime(2022, 7, 1, tzinfo=timezone.utc), as_of=AS_OF,
    )
    fresh = recency_score(
        n30=4, n90=8, n180=8, n365=8,
        most_recent=datetime(2026, 9, 1, tzinfo=timezone.utc), as_of=AS_OF,
    )
    assert stale < 15
    assert fresh > 70
    assert fresh > stale * 4


def test_fixture_not_same_as_civil():
    fixture = label_demand(
        permit_type="Plumbing",
        description="Grease interceptor replacement",
        project_category="Plumbing",
    )
    civil = label_demand(
        permit_type="PLMB",
        description="ONSITE WATER AND SEWER",
        project_category="Plumbing",
    )
    gas = label_demand(
        permit_type="Residential",
        description="Gas line from meter to water heater",
        project_category="Residential",
    )
    assert "plumbing_fixture" in fixture
    assert "civil_sewer" in civil or "site_utility" in civil
    assert "plumbing_fixture" not in civil
    assert "fuel_gas" in gas
    assert primary_category(fixture) != primary_category(civil)


def test_plmb_type_is_not_automatically_fixture():
    labels = label_demand(
        permit_type="PLMB",
        description="CONFIDENTIAL: PH3 CUPB PLM381 COMMERCIAL NEW",
        project_category="Plumbing",
    )
    assert "plumbing_fixture" not in labels
    assert "commercial_plumbing" in labels or "unknown_wet_scope" in labels


def test_gc_cm_not_treated_as_plumbing_specialist():
    caps = [
        {"capability": "plumbing", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
        {"capability": "electrical", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
        {"capability": "hvac_mechanical", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
    ]
    assert looks_like_gc_cm("United Integrated Services (USA) Corp", caps)
    ident = infer_trade_identity(company_name="Austin Commercial", caps=caps)
    assert ident["trade_identity"] == "gc_with_plumbing_demand"
    plumber = infer_trade_identity(
        company_name="Kerns Plumbing LLC",
        caps=[{"capability": "plumbing", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"}],
    )
    assert plumber["trade_identity"] == "plumbing_specialist"
    assert plumber["trade_identity_score"] > ident["trade_identity_score"]
    parker = infer_trade_identity(
        company_name="PARKER & SONS, INC",
        caps=[
            {"capability": "plumbing", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
            {"capability": "fuel_gas", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
            {"capability": "hvac_mechanical", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
            {"capability": "electrical", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
            {"capability": "general_contractor", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
        ],
    )
    assert parker["trade_identity"] == "plumbing_specialist"
    fire = infer_trade_identity(
        company_name="COMPLETE FIRE PROTECTION, INC.",
        caps=[{"capability": "plumbing", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"}],
    )
    assert fire["trade_identity"] == "other_trade"
    propane = infer_trade_identity(
        company_name="Arizona Propane",
        caps=[
            {"capability": "plumbing", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
            {"capability": "fuel_gas", "confidence": 100, "capability_class": "specialist_capability", "attribution_role": "trade_contractor"},
        ],
    )
    assert propane["trade_identity"] == "fuel_gas_specialist"


def test_plumber_outranks_plmb_gc_and_toyotalift():
    c = conn()
    plumber = _company(c, "Kerns Plumbing LLC")
    _cap(c, plumber, "plumbing")
    _cap(c, plumber, "fuel_gas")
    for i in range(6):
        _job(
            c, plumber, num=f"K{i}", permit_type="Plumbing",
            description="Water heater replacement and re-pipe",
            category="Plumbing", relevance=81.6, issued=f"2026-09-0{i+1}",
        )

    gc = _company(c, "United Integrated Services USA Corp")
    _cap(c, gc, "plumbing")
    _cap(c, gc, "electrical")
    _cap(c, gc, "hvac_mechanical")
    for i in range(12):
        _job(
            c, gc, num=f"U{i}", permit_type="PLMB",
            description=f"CONFIDENTIAL: PH3 PLM{i} COMMERCIAL NEW",
            category="Plumbing", relevance=84.3, issued="2026-04-01",
        )

    lift = _company(c, "TOYOTALIFT OF ARIZONA INC")
    _cap(c, lift, "other", confidence=22, role="unknown_role", klass="insufficient_evidence")
    _job(
        c, lift, num="TL1", permit_type="COM",
        description="High piled storage racking and forklift aisles",
        category="Warehouse", relevance=17.7, issued="2026-04-15",
    )

    before_rel = list(c.execute(
        "SELECT project_id, relevance_score FROM project_customer_relevance ORDER BY project_id"
    ))
    before_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    score_company_accounts(c, as_of=AS_OF)
    after_rel = list(c.execute(
        "SELECT project_id, relevance_score FROM project_customer_relevance ORDER BY project_id"
    ))
    after_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    assert before_rel == after_rel
    assert before_opp == after_opp

    pr = _row(c, plumber)
    gr = _row(c, gc)
    lr = _row(c, lift)
    assert pr["account_priority_score"] > gr["account_priority_score"]
    assert lr["account_priority_score"] < 30
    assert pr["trade_identity"] == "plumbing_specialist"
    assert gr["trade_identity"] == "gc_with_plumbing_demand"
    assert pr["relevant_30d"] >= 5


def test_stale_volume_does_not_beat_recent_plumber():
    c = conn()
    stale = _company(c, "Honest Plumbing AZ LLC")
    _cap(c, stale, "plumbing")
    for i in range(8):
        _job(c, stale, num=f"S{i}", description="Sewer line repair",
             relevance=84.3, issued="2022-07-07")
    recent = _company(c, "Advanced Plumbing and Piping llc")
    _cap(c, recent, "plumbing")
    for i in range(3):
        _job(c, recent, num=f"R{i}", description="Water heater replacement",
             relevance=81.6, issued="2026-09-10")
    score_company_accounts(c, as_of=AS_OF)
    assert _row(c, recent)["account_priority_score"] > _row(c, stale)["account_priority_score"]


def test_idempotent_and_no_max_plateau_sort_key():
    c = conn()
    a = _company(c, "Gas Piping Inc")
    _cap(c, a, "fuel_gas")
    _cap(c, a, "plumbing")
    _job(c, a, num="G1", permit_type="Residential",
         description="Gas line from meter to water heater", category="Residential",
         relevance=81.3, issued="2026-08-15")
    b = _company(c, "Brincor LLC")
    _cap(c, b, "plumbing")
    _job(c, b, num="B1", description="Re-pipe and fixture replacement",
         relevance=81.3, issued="2026-09-01")
    first = score_company_accounts(c, as_of=AS_OF)
    second = score_company_accounts(c, as_of=AS_OF)
    assert first["accounts"] == second["accounts"] == 2
    assert first["relevance_rows_written"] == 0
    scores = [r["account_priority_score"] for r in c.execute(
        "SELECT account_priority_score FROM company_customer_priority"
    )]
    dist = score_distribution(scores)
    assert dist["unique"] == 2


def test_municipality_stays_low():
    c = conn()
    cid = _company(c, "CITY OF PHOENIX WATER SERVICES")
    _cap(c, cid, "plumbing")
    _job(c, cid, num="M1", description="ANNUAL FACILITIES PERMIT", relevance=84.3)
    score_company_accounts(c, as_of=AS_OF)
    row = _row(c, cid)
    assert row["trade_identity"] == "municipality"
    assert row["account_priority_score"] < 40


def test_serializers_and_dashboard_ignore_account_priority():
    src = inspect.getsource(crm_service.opportunities)
    assert "account_priority_score" not in src
    assert "relevance_score" not in src
    fields = serializers._COMPANY_FIELDS + serializers._INTEL_FIELDS + serializers._PROJECT_FIELDS
    assert "account_priority_score" not in fields
    assert "customer_relevance_score" not in fields
    assert "trade_identity" not in fields
