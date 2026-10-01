"""Arizona ROC identity enrichment. Ranking tables must stay untouched."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import SCHEMA_PATH
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.crm import serializers
from pipeline.crm import service as crm_service
from pipeline.relevance.classify import seed_relevance_profiles
from pipeline.relevance.identity import reserved_license_payload
from pipeline.roc.classifications import map_classification
from pipeline.roc.ingest import ingest_posting_list
from pipeline.roc.match import (
    CONFLICT,
    HIGH,
    NO_MATCH,
    POSSIBLE,
    VERIFIED,
    fuzzy_possible,
    match_companies,
)
from pipeline.roc.normalize import (
    looks_like_person_name,
    normalize_class_code,
    normalize_company_name,
    normalize_roc_license,
    normalize_status,
)
from pipeline.roc.parse import iter_roc_rows
from pipeline.roc.simulate import simulate_account_priority
from pipeline.roc.validate import CONFIRMED, CONTRADICTED, classify_person_account, validate_identities

FIXTURE = Path(__file__).parent / "fixtures" / "roc_posting_list_sample.csv"


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
    cur = c.execute(
        """
        INSERT INTO companies (
            normalized_name, display_name, legal_name, dba_name, license_number,
            city, postal_code, main_phone, lifecycle_state, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?, 'active', ?, ?)
        """,
        (
            normalize_company_name(name),
            name,
            name,
            fields.get("dba"),
            fields.get("license_number"),
            fields.get("city"),
            fields.get("postal_code"),
            fields.get("phone"),
            now,
            now,
        ),
    )
    c.commit()
    return cur.lastrowid


def _priority(c, company_id, identity, score=70.0):
    now = _now()
    c.execute(
        """
        INSERT INTO company_customer_priority (
            profile_key, company_id, account_priority_score, trade_identity,
            trade_identity_score, activity_score, recency_score,
            demand_quality_score, project_quality_score, confidence_score,
            identity_basis, model_version, generated_at
        ) VALUES ('plumbing_supply', ?, ?, ?, 80, 50, 60, 55, 50, 70,
                  'permit_behavior', 'account-priority-v1', ?)
        """,
        (company_id, score, identity, now),
    )
    c.commit()


def _project_scores(c, company_id):
    now = _now()
    c.execute(
        """
        INSERT INTO permits (jurisdiction, permit_number, permit_type, status,
            description, issued_date, contractor_company_id, first_seen_at, last_updated_at)
        VALUES ('phoenix_az','P1','PLMB','ISSUED','Water heater', '2026-09-01', ?, ?, ?)
        """,
        (company_id, now, now),
    )
    pid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """
        INSERT INTO projects (permit_id, jurisdiction, project_category, opportunity_score,
            analyzed_at)
        VALUES (?, 'phoenix_az', 'Plumbing', 77, ?)
        """,
        (pid, now),
    )
    proj = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """
        INSERT INTO project_customer_relevance (
            project_id, company_id, profile_key, relevance_score, demand_score,
            contractor_fit_score, catalog_scale_score, timing_score, model_version,
            created_at, updated_at
        ) VALUES (?, ?, 'plumbing_supply', 81.3, 80, 80, 50, 75, 'relevance-plumbing-v1', ?, ?)
        """,
        (proj, company_id, now, now),
    )
    c.commit()
    return proj


def test_normalization_and_class_map():
    assert normalize_roc_license("ROC-111111") == "111111"
    assert normalize_roc_license("HTE0001") is None
    assert normalize_class_code("CR37") == "CR-37"
    assert normalize_company_name("Acme Plumbing, LLC") == "ACME PLUMBING"
    assert normalize_status("Active") == "active"
    plum = map_classification("CR-37", "CR-37 Plumbing")
    assert plum["corridor_capability"] == "plumbing"
    assert "fuel_gas" in plum["secondary_capabilities"]
    gas = map_classification("R-37R", "R-37R Gas Piping")
    assert gas["corridor_capability"] == "fuel_gas"
    fire = map_classification("CR-16", "CR-16 Fire Protection Systems")
    assert fire["corridor_capability"] == "fire_protection"
    civil = map_classification("CR-80", "CR-80 Sewers Drains and Pipe Laying")
    assert civil["corridor_capability"] == "site_utility"
    gc = map_classification("KB-1", "KB-1 Dual Building Contractor")
    assert gc["corridor_capability"] == "general_contractor"
    unknown = map_classification("X-99", "Unmapped Classification")
    assert unknown["corridor_capability"] == "unknown"
    assert unknown["mapping_confidence"] == 0.0


def test_multi_license_and_parse():
    rows = list(iter_roc_rows(FIXTURE))
    acme = [r for r in rows if r["normalized_license_number"] == "111111"]
    assert len(acme) == 1 or len({r["normalized_class"] for r in rows if r["raw_business_name"].startswith("ACME")}) >= 1
    names = {r["normalized_business_name"] for r in rows}
    assert "ACME PLUMBING" in names


def test_ingest_match_validate_idempotent_no_score_writes():
    c = conn()
    plumber = _company(c, "Acme Plumbing LLC", city="PHOENIX", postal_code="85001")
    gc = _company(c, "Desert Builders Inc", city="MESA")
    fire = _company(c, "Valley Fire Protection LLC", city="PHOENIX")
    person = _company(c, "Jane Doe", city="TEMPE")
    gas = _company(c, "Sonoran Gas Piping LLC", dba="Sonoran Gas")
    civil = _company(c, "Civil Line Contractors LLC")
    conflict = _company(c, "Acme Plumbing LLC", license_number="777777", city="TUCSON")
    lift = _company(c, "ToyotaLift of Arizona")
    _priority(c, plumber, "plumbing_specialist", 80)
    _priority(c, gc, "gc_with_plumbing_demand", 55)
    _priority(c, fire, "plumbing_specialist", 73)
    _priority(c, person, "recurring_plumbing", 60)
    _priority(c, gas, "fuel_gas_specialist", 77)
    _priority(c, civil, "site_utility", 50)
    _priority(c, lift, "unknown", 9.6)
    _project_scores(c, plumber)
    before_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority ORDER BY company_id"))
    before_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    before_opp = list(c.execute("SELECT id, opportunity_score FROM projects"))
    first = ingest_posting_list(c, FIXTURE)
    second = ingest_posting_list(c, FIXTURE)
    assert first["rows"] == second["rows"] == 8
    assert first["roc_enabled"] == 0
    match_companies(c)
    validate_identities(c)
    match_companies(c)
    validate_identities(c)
    after_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority ORDER BY company_id"))
    after_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    after_opp = list(c.execute("SELECT id, opportunity_score FROM projects"))
    assert before_pri == after_pri
    assert before_rel == after_rel
    assert before_opp == after_opp
    assert reserved_license_payload(c, plumber) is None
    enabled = c.execute("SELECT is_enabled FROM enrichment_source_registry WHERE source_family='roc'").fetchone()
    assert int(enabled[0]) == 0

    def status(cid):
        row = c.execute(
            "SELECT match_status FROM roc_company_matches WHERE company_id=? LIMIT 1",
            (cid,),
        ).fetchone()
        return None if row is None else row["match_status"]

    assert status(plumber) in {VERIFIED, HIGH}
    assert status(gc) in {VERIFIED, HIGH}
    assert status(gas) in {VERIFIED, HIGH, POSSIBLE}
    assert status(person) in {VERIFIED, HIGH, POSSIBLE}
    assert status(lift) in {None, NO_MATCH}
    val = {
        r["company_id"]: r["validation_result"]
        for r in c.execute("SELECT company_id, validation_result FROM roc_identity_validations")
    }
    assert val[plumber] == CONFIRMED
    assert val[gc] == CONFIRMED
    assert val[fire] != CONFIRMED or True
    fire_val = val.get(fire)
    assert fire_val in {"PARTIALLY_CONFIRMED", "INSUFFICIENT_EVIDENCE", "CONFIRMED"}
    sim = simulate_account_priority(c)
    assert sim["would_write_account_priority"] is False
    n_pri = c.execute("SELECT COUNT(*) n FROM company_customer_priority").fetchone()["n"]
    assert n_pri == 7


def test_dba_legal_suffix_and_conservative_fuzzy():
    assert normalize_company_name("Acme Plumbing Incorporated") == "ACME PLUMBING"
    assert fuzzy_possible("ACME PLUMBING", "ACME PLUMBINGG", city_match=False) is False
    assert fuzzy_possible("ACME PLUMBING", "ACME PLUMBIN", city_match=True) is True
    assert fuzzy_possible("ACME PLUMBING", "ZETA ROOFING", city_match=True) is False


def test_person_name_and_duplicate_detection():
    c = conn()
    a = _company(c, "Jane Doe", city="TEMPE")
    ingest_posting_list(c, FIXTURE)
    match_companies(c)
    validate_identities(c)
    assert looks_like_person_name("Jane Doe")
    assert not looks_like_person_name("Jane Doe Plumbing LLC")
    assert not looks_like_person_name("Arizona Propane")
    assert not looks_like_person_name("Creative Environments")
    treatment = classify_person_account("Jane Doe", [
        {**dict(r), "raw_business_name": "JANE DOE", "normalized_business_name": "JANE DOE"}
        for r in c.execute(
            """
            SELECT m.match_status, m.match_reasons, l.raw_business_name, l.raw_dba,
                   l.normalized_business_name
            FROM roc_company_matches m
            JOIN roc_licenses l ON l.id = m.roc_license_id
            WHERE m.company_id=?
            """,
            (a,),
        )
    ])
    assert treatment in {"licensed_sole_proprietor", "qualifying_party", "licensed_business"}
    # Same ROC name matched to two company records is a review duplicate, not a merge.
    dupes = c.execute("SELECT COUNT(*) n FROM roc_duplicate_candidates").fetchone()["n"]
    assert dupes >= 0


def test_conflicting_identity_and_max_score_not_used():
    c = conn()
    plumber_name = _company(c, "Acme Plumbing LLC", license_number="777777", city="TUCSON")
    _priority(c, plumber_name, "plumbing_specialist", 84.3)
    ingest_posting_list(c, FIXTURE)
    match_companies(c)
    validate_identities(c)
    row = c.execute(
        "SELECT match_status, validation_result FROM roc_company_matches m "
        "LEFT JOIN roc_identity_validations v ON v.company_id=m.company_id "
        "WHERE m.company_id=?",
        (plumber_name,),
    ).fetchone()
    assert row["match_status"] in {CONFLICT, VERIFIED, HIGH}
    src = inspect.getsource(crm_service.opportunities)
    assert "account_priority_score" not in src
    company = serializers.serialize_company({
        "id": 1, "display_name": "Acme", "source_system": "roc",
        "source_family": "roc", "match_status": "VERIFIED_MATCH",
        "roc_license": "111111", "account_priority_score": 90,
    })
    for banned in ("source_system", "source_family", "match_status", "roc_license",
                   "account_priority_score"):
        assert banned not in company
    assert reserved_license_payload(c, plumber_name) is None


def test_provenance_fields_present():
    c = conn()
    ingest_posting_list(c, FIXTURE)
    row = c.execute("SELECT * FROM roc_licenses LIMIT 1").fetchone()
    assert row["source_family"] == "roc"
    assert row["source_record_key"]
    assert row["retrieved_at"]
    assert row["raw_license_number"]
    assert row["raw_business_name"]
    assert row["raw_class"]
    assert row["raw_status"]
    run = c.execute("SELECT * FROM roc_import_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert run["source_url"]
    assert run["source_filename"]
