"""Contractor intelligence foundation — taxonomy, detection, confidence, safety."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from pipeline.config.settings import SCHEMA_PATH
from pipeline.contractor_intel.classify import classify_companies
from pipeline.contractor_intel.confidence import Evidence, compute_confidence
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.contractor_intel.signals import extract_signals, signals_from_description
from pipeline.contractor_intel.taxonomy import (
    CAPABILITIES,
    CAPABILITY_SET,
    TRADE_CAPABILITIES,
)
from pipeline.crm import serializers


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@pytest.fixture()
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
            category="Other", score=70.0, issued="2026-08-01"):
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
            project_category, opportunity_score, opportunity_date, analysis_version, analyzed_at)
        VALUES (?, 'phoenix_az', ?, ?, ?, ?, 'test', ?)
        """,
        (permit_id, company_id, category, score, issued, now),
    )
    c.commit()
    return permit_id


def test_taxonomy_contains_required_capabilities():
    required = {
        "plumbing", "fuel_gas", "hvac_mechanical", "site_utility",
        "general_contractor", "electrical", "roofing", "concrete",
        "framing", "drywall", "flooring", "cabinetry", "other", "multi_trade",
    }
    assert required <= CAPABILITY_SET
    assert "fuel_gas" in TRADE_CAPABILITIES
    assert "multi_trade" not in TRADE_CAPABILITIES
    assert "other" not in TRADE_CAPABILITIES
    assert len(CAPABILITIES) == len(set(CAPABILITIES))


def test_gas_detected_without_gas_in_company_name():
    hits = extract_signals(
        permit_type="Plumbing",
        description="Install CSST from meter to gas water heater",
        project_category="Plumbing",
        company_name="Desert Pipe Co",
    )
    caps = {h.capability for h in hits}
    assert "fuel_gas" in caps
    assert "plumbing" in caps


def test_generic_gas_token_blocked_for_gas_station():
    hits = signals_from_description("New canopy at gas station convenience store")
    assert not any(h.capability == "fuel_gas" for h in hits)


def test_false_positive_gasket_vegas_gasoline():
    for text in ("Replace valve gasket", "Las Vegas warehouse TI", "gasoline canopy"):
        caps = {h.capability for h in signals_from_description(text)}
        assert "fuel_gas" not in caps, text


def test_plumbing_from_permit_type_and_description():
    hits = extract_signals(
        permit_type="Plumbing - Residential",
        description="Water heater replacement and re-pipe",
        project_category="Plumbing",
        company_name="Acme Services LLC",
    )
    plumbing = [h for h in hits if h.capability == "plumbing"]
    assert plumbing
    types = {h.signal_type for h in plumbing}
    assert "permit_type" in types
    assert "description_phrase" in types


def test_confidence_repeat_activity_raises_score():
    weak = [
        Evidence("plumbing", "description_phrase", "plumbing", 22, permit_id=1),
    ]
    strong = weak + [
        Evidence("plumbing", "permit_type", "plumbing", 25, permit_id=2),
        Evidence("plumbing", "description_phrase", "water heater", 18, permit_id=3),
        Evidence("plumbing", "description_phrase", "plumbing", 22, permit_id=4),
        Evidence("plumbing", "description_phrase", "backflow", 18, permit_id=5),
    ]
    assert compute_confidence(strong) > compute_confidence(weak)
    assert compute_confidence(strong) >= 75


def test_name_only_confidence_capped():
    name_only = [
        Evidence("plumbing", "company_name", "plumb", 12, permit_id=1),
    ]
    assert compute_confidence(name_only) <= 26


def test_lifestyle_only_fuel_gas_capped():
    lifestyle = [
        Evidence("fuel_gas", "description_phrase", "bbq", 12, permit_id=1),
        Evidence("fuel_gas", "description_phrase", "fire pit", 12, permit_id=1),
    ]
    assert compute_confidence(lifestyle) <= 42


def test_single_generic_gas_token_not_proof():
    one = [Evidence("fuel_gas", "description_phrase", "gas", 10, permit_id=1)]
    assert compute_confidence(one) == 0


def test_generic_gas_does_not_raise_score():
    strong = [
        Evidence("fuel_gas", "description_phrase", "gas line", 24, permit_id=1,
                 evidence_directness="direct"),
    ]
    with_generic = strong + [
        Evidence("fuel_gas", "description_phrase", "gas", 10, permit_id=1),
        Evidence("fuel_gas", "description_phrase", "gas", 10, permit_id=2),
        Evidence("fuel_gas", "description_phrase", "gas", 10, permit_id=3),
    ]
    assert compute_confidence(with_generic) == compute_confidence(strong)


def test_multi_label_and_multi_trade(conn):
    cid = _company(conn, "Valley Mechanical Piping LLC")
    _permit(conn, cid, num="P1", permit_type="Plumbing",
            description="Gas line and water heater", category="Plumbing", score=81)
    _permit(conn, cid, num="P2", permit_type="Mechanical",
            description="HVAC RTU replacement", category="Mechanical", score=77)
    classify_companies(conn)
    caps = {
        r["capability"]: r["confidence"]
        for r in conn.execute(
            "SELECT capability, confidence FROM company_capabilities WHERE company_id=?",
            (cid,),
        )
    }
    assert "plumbing" in caps
    assert "fuel_gas" in caps
    assert "hvac_mechanical" in caps
    assert "multi_trade" in caps
    assert caps["multi_trade"] <= 95


def test_does_not_change_opportunity_scores(conn):
    cid = _company(conn, "Score Lock Plumbing")
    _permit(conn, cid, num="S1", permit_type="Plumbing",
            description="New plumbing", category="Plumbing", score=88.5)
    before = conn.execute("SELECT opportunity_score FROM projects").fetchone()[0]
    classify_companies(conn)
    after = conn.execute("SELECT opportunity_score FROM projects").fetchone()[0]
    assert after == before == 88.5


def test_idempotent_rerun(conn):
    cid = _company(conn, "Repeat Plumbing Inc")
    _permit(conn, cid, num="R1", permit_type="Plumbing",
            description="Sewer and water line", category="Plumbing")
    a = classify_companies(conn)
    b = classify_companies(conn)
    assert a["capability_rows"] == b["capability_rows"]
    assert a["evidence_rows"] == b["evidence_rows"]
    n = conn.execute("SELECT COUNT(*) n FROM company_capabilities").fetchone()["n"]
    assert n == a["capability_rows"]


def test_schema_tables_exist(conn):
    names = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert "company_capabilities" in names
    assert "company_capability_evidence" in names
    assert "company_enrichment" in names
    assert "enrichment_source_registry" in names
    enabled = {
        r["source_family"]: r["is_enabled"]
        for r in conn.execute(
            "SELECT source_family, is_enabled FROM enrichment_source_registry"
        )
    }
    assert enabled.get("roc") == 0
    assert enabled.get("acc") == 0
    assert enabled.get("ucc") == 0
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(company_capabilities)")}
    assert "attribution_role" in cols
    assert "capability_class" in cols
    ev_cols = {r["name"] for r in conn.execute("PRAGMA table_info(company_capability_evidence)")}
    assert "attribution_role" in ev_cols
    assert "evidence_directness" in ev_cols


def test_placeholder_company_skipped(conn):
    cid = _company(conn, "OWNER")
    _permit(conn, cid, num="O1", permit_type="Plumbing", description="Gas line")
    classify_companies(conn)
    n = conn.execute(
        "SELECT COUNT(*) n FROM company_capabilities WHERE company_id=?", (cid,)
    ).fetchone()["n"]
    assert n == 0


def test_unlinked_projects_are_not_classified(conn):
    cid = _company(conn, "Ghost Electric")
    now = _now()
    conn.execute(
        """
        INSERT INTO permits (jurisdiction, permit_number, permit_type, description,
            first_seen_at, last_updated_at)
        VALUES ('phoenix_az','U1','Electrical','Panel upgrade', ?, ?)
        """,
        (now, now),
    )
    pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO projects (permit_id, jurisdiction, project_category, opportunity_score, analyzed_at)
        VALUES (?, 'phoenix_az', 'Electrical', 60, ?)
        """,
        (pid, now),
    )
    conn.commit()
    classify_companies(conn)
    n = conn.execute(
        "SELECT COUNT(*) n FROM company_capabilities WHERE company_id=?", (cid,)
    ).fetchone()["n"]
    assert n == 0


def test_serializers_hide_internal_intel_fields():
    blob = {
        "id": 1,
        "display_name": "Acme",
        "source_system": "roc",
        "source_record_id": "x",
        "capability": "fuel_gas",
        "classification_source": "description_phrase",
        "roc": "secret",
        "acc": "secret",
        "ucc": "secret",
        "company_priority_score": 50,
        "total_projects": 3,
    }
    company = serializers.serialize_company(blob)
    intel = serializers.serialize_intelligence(blob)
    for banned in ("capability", "classification_source", "roc", "acc", "ucc",
                   "source_system", "source_record_id", "attribution_role",
                   "capability_class"):
        assert banned not in company
        assert banned not in intel


def _caps(conn, company_id):
    return {
        r["capability"]: dict(r)
        for r in conn.execute(
            """
            SELECT capability, confidence, attribution_role, capability_class
            FROM company_capabilities WHERE company_id=?
            """,
            (company_id,),
        )
    }


def test_role_taxonomy_complete():
    from pipeline.contractor_intel.taxonomy import ATTRIBUTION_ROLE_SET, CAPABILITY_CLASS_SET
    assert ATTRIBUTION_ROLE_SET == {
        "trade_contractor", "gc_of_record", "subcontractor_if_known",
        "owner_builder", "developer", "architect_engineer_if_present",
        "unknown_role",
    }
    assert CAPABILITY_CLASS_SET == {
        "specialist_capability", "recurring_trade_capability",
        "incidental_project_scope", "insufficient_evidence",
    }


def test_role_attribution_production_builder_vs_trade():
    from pipeline.contractor_intel.roles import infer_profile
    shea = infer_profile(
        company_name="SHEA HOMES",
        permit_types=["RES", "RES", "Residential"] * 10,
        descriptions=["New 2536 SF two story residence with garage"] * 5,
    )
    assert shea.role == "gc_of_record"
    assert shea.kind == "production_builder"
    shea_plants = infer_profile(
        company_name="SHEA HOMES",
        permit_types=["RES"] * 20 + ["NATIVE PLANT"] * 18,
        descriptions=["New 2536 SF two story residence with garage"] * 5,
    )
    assert shea_plants.kind == "production_builder"
    assert shea_plants.role == "gc_of_record"
    kerns = infer_profile(
        company_name="KERNS PLUMBING",
        permit_types=["MINIMUM (PLUMBING)"] * 8,
        descriptions=["Install water heater and re-pipe"] * 4,
    )
    assert kerns.role == "trade_contractor"
    pegasus = infer_profile(
        company_name="PEGASUS POOLS",
        permit_types=["RES"],
        descriptions=["CONSTRUCT A 911 SF SWIMMING POOL WITH NATURAL GAS HEAT"],
    )
    assert pegasus.kind == "pool"


def test_gc_of_record_suppressed_for_fuel_gas(conn):
    for name, num in (
        ("Lennar Arizona Cons. Co", "L"),
        ("SHEA HOMES", "S"),
        ("HIGLEY HOMES, LLC", "H"),
    ):
        cid = _company(conn, name)
        for i in range(8):
            _permit(
                conn, cid, num=f"{num}{i}", permit_type="RES",
                description=(
                    f"New 2500 SF two story residence includes a new electric, "
                    f"gas cooktop, gas meter, gas dryer and BBQ"
                ),
                category="Residential",
            )
        classify_companies(conn)
        caps = _caps(conn, cid)
        gas = caps.get("fuel_gas")
        if gas:
            assert gas["confidence"] < 75, name
            assert gas["attribution_role"] == "gc_of_record"
            assert gas["capability_class"] == "incidental_project_scope"
        assert (caps.get("fuel_gas") or {}).get("confidence", 0) <= 36


def test_production_builder_false_positive_volume_does_not_go_high(conn):
    cid = _company(conn, "LENNAR ARIZONA CONS. CO")
    for i in range(40):
        _permit(
            conn, cid, num=f"LN{i}", permit_type="RES",
            description="New 3505 SF two story residence. Includes gas cooktop and gas meter.",
        )
    classify_companies(conn)
    gas = _caps(conn, cid).get("fuel_gas")
    if gas:
        assert gas["confidence"] < 50
        assert gas["capability_class"] != "specialist_capability"


def test_specialist_vs_incidental_scope():
    from pipeline.contractor_intel.confidence import capability_class
    from pipeline.contractor_intel.roles import CompanyProfile
    trade = CompanyProfile("trade_contractor", 86, "trade")
    gc = CompanyProfile("gc_of_record", 92, "production_builder")
    ev = [
        Evidence("fuel_gas", "description_phrase", "gas line", 24, permit_id=i,
                 evidence_directness="direct")
        for i in range(1, 6)
    ]
    assert capability_class(90, ev, profile=trade) == "specialist_capability"
    indirect = [
        Evidence("fuel_gas", "description_phrase", "gas cooktop", 6, permit_id=i,
                 evidence_directness="indirect")
        for i in range(1, 6)
    ]
    score = compute_confidence(indirect, profile=gc)
    assert score <= 36
    assert capability_class(score, indirect, profile=gc) == "incidental_project_scope"


def test_pool_and_landscape_plumbing_not_specialist(conn):
    cid = _company(conn, "CREATIVE ENVIRONMENTS")
    _permit(conn, cid, num="CE1", permit_type="MINIMUM (PLUMBING)",
            description="CONSTRUCT A 558 SF SWIMMING POOL & SPA", category="Other")
    _permit(conn, cid, num="CE2", permit_type="SWIMMING POOL W/SPA",
            description="CONSTRUCT A 558 SF SWIMMING POOL", category="Other")
    _permit(conn, cid, num="CE3", permit_type="MINIMUM (PLUMBING)",
            description="Pool equipment plumbing", category="Other")
    classify_companies(conn)
    caps = _caps(conn, cid)
    plum = caps.get("plumbing")
    if plum:
        assert plum["confidence"] < 75
        assert plum["capability_class"] != "specialist_capability"

    pid = _company(conn, "PEGASUS POOLS")
    _permit(
        conn, pid, num="PG1", permit_type="RES",
        description="CONSTRUCT A 911 SF SWIMMING POOL & 64 SF SPA WITH NATURAL GAS HEAT.",
    )
    classify_companies(conn)
    pg = _caps(conn, pid).get("fuel_gas")
    if pg:
        assert pg["confidence"] < 75
        assert pg["capability_class"] != "specialist_capability"
    plum_pg = _caps(conn, pid).get("plumbing")
    if plum_pg:
        assert plum_pg["confidence"] < 75

    land = _company(conn, "Desert Landscape Irrigation")
    _permit(conn, land, num="LS1", permit_type="NATIVE PLANT",
            description="Irrigation and native plant with water line stub")
    classify_companies(conn)
    lp = _caps(conn, land).get("plumbing")
    if lp:
        assert lp["capability_class"] != "specialist_capability"


def test_pool_company_with_real_gas_line_is_recurring_not_gc(conn):
    cid = _company(conn, "CREATIVE ENVIRONMENTS")
    for i in range(4):
        _permit(
            conn, cid, num=f"GL{i}", permit_type="RES",
            description=(
                "INSTALLATION OF 63 FT OF 2 INCH PE GAS LINE BURIED 18 INCH "
                "WITH TRACER WIRE FROM GAS METER TO FIRE PIT"
            ),
        )
    classify_companies(conn)
    gas = _caps(conn, cid).get("fuel_gas")
    assert gas is not None
    assert gas["attribution_role"] == "trade_contractor"
    assert gas["capability_class"] in {
        "recurring_trade_capability", "specialist_capability",
    }
    assert gas["capability_class"] != "incidental_project_scope"


def test_legitimate_multi_trade_still_fires(conn):
    cid = _company(conn, "Valley Mechanical Piping LLC")
    _permit(conn, cid, num="MT1", permit_type="Plumbing",
            description="Gas line and water heater replacement", category="Plumbing")
    _permit(conn, cid, num="MT2", permit_type="Mechanical",
            description="HVAC RTU replacement", category="Mechanical")
    classify_companies(conn)
    caps = _caps(conn, cid)
    assert "plumbing" in caps
    assert "fuel_gas" in caps
    assert "hvac_mechanical" in caps
    assert "multi_trade" in caps
    assert caps["plumbing"]["attribution_role"] == "trade_contractor"
    assert caps["plumbing"]["capability_class"] in {
        "specialist_capability", "recurring_trade_capability",
    }


def test_evidence_records_role_and_directness(conn):
    cid = _company(conn, "Desert Pipe Co")
    _permit(conn, cid, num="DP1", permit_type="Plumbing - Residential",
            description="Install CSST from meter to gas water heater", category="Plumbing")
    classify_companies(conn)
    row = conn.execute(
        """
        SELECT attribution_role, attribution_confidence, evidence_directness
        FROM company_capability_evidence
        WHERE company_id=? AND capability='fuel_gas'
        LIMIT 1
        """,
        (cid,),
    ).fetchone()
    assert row is not None
    assert row["attribution_role"] in {
        "trade_contractor", "unknown_role", "gc_of_record",
    }
    assert row["evidence_directness"] in {"direct", "indirect"}
    assert row["attribution_confidence"] is not None
