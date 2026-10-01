"""Phase 4F internal sales lanes. Ranking tables stay untouched."""

from __future__ import annotations

import inspect
import sqlite3
from datetime import datetime, timezone

from pipeline.company_resolution.merge import merge_companies
from pipeline.config.settings import SCHEMA_PATH
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.crm import serializers
from pipeline.crm import service as crm_service
from pipeline.relevance.classify import seed_relevance_profiles
from pipeline.roc.normalize import normalize_company_name
from pipeline.sales_lanes.assign import classify_features, presentable_in_lane
from pipeline.sales_lanes.book import build_lane_book, persist_books
from pipeline.sales_lanes.collapse import collapse_stats, presentation_groups
from pipeline.sales_lanes.lanes import (
    CIVIL_WET_UTILITY,
    FIRE_BACKFLOW,
    FUEL_GAS_PROPANE,
    GAS_PIPING_CONTRACTOR,
    GENERAL_CONTRACTOR_CM,
    HIGH,
    HVAC_MECHANICAL,
    IDENTITY_REVIEW,
    LOW,
    MEDIUM,
    MIXED_PLUMBING_GAS,
    MULTI_TRADE,
    PLUMBING_CORE,
    PROPANE_RETAIL_EXCHANGE,
    PROPANE_TANK_INSTALL,
    SPECIALTY_OTHER,
)
from pipeline.sales_lanes.serialize import (
    BANNED_PUBLIC_FIELDS,
    serialize_internal_card,
    serialize_internal_detail,
)
from pipeline.sales_lanes.store import assign_all
from pipeline.sales_lanes.wording import identity_safe


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
        INSERT INTO companies (normalized_name, display_name, legal_name,
            lifecycle_state, created_at, updated_at)
        VALUES (?,?,?, 'active', ?, ?)
        """,
        (normalize_company_name(name), name, name, now, now),
    )
    c.commit()
    return cur.lastrowid


def _priority(c, company_id, identity="plumbing_specialist", score=80.0, **extra):
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
                  'permit_behavior', 'test why now',
                  ?, ?, 'account-priority-v1', ?)
        """,
        (
            company_id,
            score,
            identity,
            extra.get("n30", 2),
            extra.get("n90", 4),
            extra.get("n180", 6),
            extra.get("n_gas", 0),
            extra.get("n_plum", 4),
            extra.get("n_rel", 10),
            extra.get("primary", "plumbing_service"),
            extra.get("demand", '{"plumbing_service": 4}'),
            now,
        ),
    )
    c.commit()


def _roc(c, company_id, klass, license_number="L1"):
    now = _now()
    c.execute(
        """
        INSERT INTO roc_licenses (
            source_record_key, retrieved_at, normalized_license_number,
            raw_class, normalized_class, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (f"{license_number}:{klass}", now, license_number, klass, klass, now, now),
    )
    lid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """
        INSERT INTO roc_company_matches (
            company_id, roc_license_id, match_status, match_confidence,
            matching_version, created_at, updated_at
        ) VALUES (?, ?, 'VERIFIED_MATCH', 95, 'roc-match-v1', ?, ?)
        """,
        (company_id, lid, now, now),
    )
    c.commit()
    return lid


def _project(c, company_id, score=74.2):
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
        INSERT INTO projects (permit_id, jurisdiction, project_category,
            opportunity_score, opportunity_date, analyzed_at)
        VALUES (?, 'phoenix_az', 'Plumbing', ?, '2026-09-01', ?)
        """,
        (pid, score, now),
    )
    prid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    c.execute(
        """
        INSERT INTO project_customer_relevance (
            project_id, company_id, profile_key, relevance_score, demand_score,
            contractor_fit_score, catalog_scale_score, timing_score,
            model_version, created_at, updated_at
        ) VALUES (?, ?, 'plumbing_supply', 80, 80, 80, 70, 70, 'relevance-plumbing-v1', ?, ?)
        """,
        (prid, company_id, now, now),
    )
    c.commit()
    return prid


def _canonical(c, name, primary_id, members):
    now = _now()
    c.execute(
        """
        INSERT INTO canonical_companies (
            canonical_name, normalized_canonical_name, primary_company_id,
            recommendation, model_version, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (name, normalize_company_name(name), primary_id, "same_entity", "entity-v1", now, now),
    )
    can_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    for cid in members:
        c.execute(
            """
            INSERT INTO company_entity_links (
                raw_company_id, canonical_company_id, relationship_type,
                match_confidence, evidence, matching_version, created_at
            ) VALUES (?,?, 'initials_variant', 90, '[]', 'entity-v1', ?)
            """,
            (cid, can_id, now),
        )
    c.commit()
    return can_id


def _keys(feat, *, presentable_only=True):
    recs = classify_features(feat)
    if presentable_only:
        return {
            r["lane_key"]
            for r in recs
            if r["fit"] in {HIGH, MEDIUM} and int(r.get("presentable") or 0)
        }
    return {r["lane_key"] for r in recs}


def _subtype(feat, lane=FUEL_GAS_PROPANE):
    recs = classify_features(feat)
    hit = next((r for r in recs if r["lane_key"] == lane), None)
    return None if hit is None else hit.get("subtype")


def test_parker_multi_lane_membership():
    feat = {
        "display_name": "PARKER & SONS, INC",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"water_heater": 161, "fuel_gas": 71, "mechanical_wet": 718}',
        "n_plum": 162,
        "n_gas": 71,
        "roc_classes": ["CR-37", "CR-39", "CR-11"],
    }
    keys = _keys(feat)
    assert PLUMBING_CORE in keys
    assert FUEL_GAS_PROPANE in keys
    assert HVAC_MECHANICAL in keys
    assert MULTI_TRADE in keys
    assert FIRE_BACKFLOW not in keys
    assert _subtype(feat) == MIXED_PLUMBING_GAS
    assert presentable_in_lane(classify_features(feat), PLUMBING_CORE)


def test_kerns_plumbing_and_gas():
    feat = {
        "display_name": "KERNS PLUMBING",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"water_heater": 1, "fuel_gas": 21}',
        "n_plum": 1,
        "n_gas": 21,
        "roc_classes": ["CR-37"],
    }
    keys = _keys(feat)
    assert keys >= {PLUMBING_CORE, FUEL_GAS_PROPANE, MULTI_TRADE}
    assert _subtype(feat) == MIXED_PLUMBING_GAS


def test_strict_plumbing_core_excludes_civil_fire_gc_propane():
    petra = {
        "display_name": "PETRA CONTRACTING",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"civil_water": 8, "civil_sewer": 4}',
        "roc_classes": ["A", "B-4"],
    }
    rci = {
        "display_name": "R C I SYSTEMS INC",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"fire_backflow": 63, "plumbing_service": 49}',
        "n_plum": 49,
        "roc_classes": ["R-16", "C-16", "CR-67"],
    }
    uis = {
        "display_name": "UNITED INTEGRATED SERVICES",
        "trade_identity": "gc_with_plumbing_demand",
        "demand_categories": '{"unknown_wet_scope": 12}',
        "roc_classes": ["B", "C-37"],
    }
    az = {
        "display_name": "Arizona Propane",
        "trade_identity": "fuel_gas_specialist",
        "demand_categories": '{"fuel_gas": 126}',
        "n_gas": 126,
        "roc_classes": ["R-37R", "CR-5"],
    }
    plumber = {
        "display_name": "KERNS PLUMBING",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"water_heater": 4}',
        "n_plum": 4,
        "roc_classes": ["CR-37"],
    }
    assert not presentable_in_lane(classify_features(petra), PLUMBING_CORE)
    assert not presentable_in_lane(classify_features(rci), PLUMBING_CORE)
    assert not presentable_in_lane(classify_features(uis), PLUMBING_CORE)
    assert not presentable_in_lane(classify_features(az), PLUMBING_CORE)
    assert presentable_in_lane(classify_features(plumber), PLUMBING_CORE)


def test_fuel_gas_lane_and_subtypes():
    az = classify_features({
        "display_name": "Arizona Propane",
        "trade_identity": "fuel_gas_specialist",
        "demand_categories": '{"fuel_gas": 126}',
        "n_gas": 126,
        "roc_classes": ["R-37R"],
    })
    rp = classify_features({
        "display_name": "RP GAS PIPING LLC",
        "trade_identity": "fuel_gas_specialist",
        "demand_categories": '{"fuel_gas": 40}',
        "n_gas": 40,
    })
    amer = classify_features({
        "display_name": "AMERIGAS PROPANE",
        "trade_identity": "fuel_gas_specialist",
        "demand_categories": '{"fuel_gas": 15}',
        "n_gas": 15,
    })
    assert presentable_in_lane(az, FUEL_GAS_PROPANE)
    assert not presentable_in_lane(az, PLUMBING_CORE)
    assert next(r["subtype"] for r in az if r["lane_key"] == FUEL_GAS_PROPANE) == PROPANE_TANK_INSTALL
    assert next(r["subtype"] for r in rp if r["lane_key"] == FUEL_GAS_PROPANE) == GAS_PIPING_CONTRACTOR
    assert next(r["subtype"] for r in amer if r["lane_key"] == FUEL_GAS_PROPANE) == PROPANE_RETAIL_EXCHANGE
    assert next(r["fit"] for r in amer if r["lane_key"] == FUEL_GAS_PROPANE) == MEDIUM


def test_fire_backflow_rci_not_plumbing():
    feat = {
        "display_name": "RCI SYSTEMS INC",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"fire_backflow": 63, "plumbing_service": 49}',
        "n_plum": 49,
        "roc_classes": ["R-16", "CR-67"],
    }
    recs = classify_features(feat)
    assert presentable_in_lane(recs, FIRE_BACKFLOW)
    assert not presentable_in_lane(recs, PLUMBING_CORE)
    assert {r["lane_key"] for r in recs if r["fit"] in {HIGH, MEDIUM}} == {FIRE_BACKFLOW}


def test_rci_with_plumbing_license_still_fire_not_core():
    feat = {
        "display_name": "RCI SYSTEMS, INC.",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"fire_backflow": 63, "plumbing_service": 49}',
        "n_plum": 49,
        "roc_classes": ["R-16", "C-16", "CR-67", "C-37"],
    }
    recs = classify_features(feat)
    assert presentable_in_lane(recs, FIRE_BACKFLOW)
    assert not presentable_in_lane(recs, PLUMBING_CORE)


def test_millennium_gas_not_mixed_plumbing():
    feat = {
        "display_name": "MILLENNIUM GAS SERVICES LLC",
        "trade_identity": "fuel_gas_specialist",
        "demand_categories": '{"fuel_gas": 56}',
        "n_gas": 56,
        "n_plum": 1,
        "roc_classes": ["CR-37", "R-37R"],
    }
    recs = classify_features(feat)
    assert presentable_in_lane(recs, FUEL_GAS_PROPANE)
    assert not presentable_in_lane(recs, PLUMBING_CORE)
    assert _subtype(feat) != MIXED_PLUMBING_GAS


def test_anybackflow_routes_to_fire():
    feat = {
        "display_name": "ANYBACKFLOW.COM INC",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"plumbing_service": 8}',
        "n_plum": 8,
        "roc_classes": ["CR-37"],
    }
    recs = classify_features(feat)
    assert presentable_in_lane(recs, FIRE_BACKFLOW)
    assert not presentable_in_lane(recs, PLUMBING_CORE)


def test_petra_civil_wet_utility():
    feat = {
        "display_name": "PETRA CONTRACTING INC",
        "trade_identity": "plumbing_specialist",
        "demand_categories": '{"civil_water": 8, "civil_sewer": 4}',
        "roc_classes": ["A"],
    }
    recs = classify_features(feat)
    assert presentable_in_lane(recs, CIVIL_WET_UTILITY)
    assert not presentable_in_lane(recs, PLUMBING_CORE)


def test_gc_cm_lane():
    feat = {
        "display_name": "AUSTIN COMMERCIAL",
        "trade_identity": "gc_with_plumbing_demand",
        "demand_categories": '{"unknown_wet_scope": 9}',
        "roc_classes": ["B"],
    }
    recs = classify_features(feat)
    assert presentable_in_lane(recs, GENERAL_CONTRACTOR_CM)
    assert not presentable_in_lane(recs, PLUMBING_CORE)


def test_not_sales_ready_person_and_expeditor():
    person = classify_features({
        "display_name": "Kris Espinoza",
        "trade_identity": "recurring_fuel_gas",
        "demand_categories": '{"fuel_gas": 18}',
        "n_gas": 18,
        "person_class": "permit_applicant",
    })
    expeditor = classify_features({
        "display_name": "RIDER PERMIT SERVICE",
        "trade_identity": "recurring_fuel_gas",
        "demand_categories": '{"fuel_gas": 25}',
        "n_gas": 25,
    })
    assert person[0]["lane_key"] == IDENTITY_REVIEW
    assert person[0]["presentable"] == 0
    assert not presentable_in_lane(person, PLUMBING_CORE)
    assert not presentable_in_lane(person, FUEL_GAS_PROPANE)
    assert expeditor[0]["lane_key"] == IDENTITY_REVIEW
    assert expeditor[0]["presentable"] == 0


def test_toyotalift_excluded_from_plumbing():
    feat = {
        "display_name": "TOYOTALIFT OF ARIZONA INC",
        "trade_identity": "gc_with_plumbing_demand",
        "demand_categories": '{"unknown_wet_scope": 2}',
    }
    recs = classify_features(feat)
    assert presentable_in_lane(recs, SPECIALTY_OTHER)
    assert not presentable_in_lane(recs, PLUMBING_CORE)
    assert not presentable_in_lane(recs, GENERAL_CONTRACTOR_CM)


def test_canonical_collapse_rci_two_rows_one_account():
    c = conn()
    a = _company(c, "RCI SYSTEMS INC")
    b = _company(c, "R C I SYSTEMS INC")
    plumber = _company(c, "KERNS PLUMBING")
    _priority(
        c, a, identity="plumbing_specialist", score=88.0,
        primary="fire_backflow", demand='{"fire_backflow": 63, "plumbing_service": 49}',
        n_plum=49,
    )
    _priority(
        c, b, identity="plumbing_specialist", score=84.0,
        primary="fire_backflow", demand='{"fire_backflow": 40, "plumbing_service": 20}',
        n_plum=20,
    )
    _priority(c, plumber, score=70.0, demand='{"water_heater": 8}', n_plum=8)
    _roc(c, a, "R-16", "ROC-A")
    _roc(c, b, "CR-67", "ROC-B")
    _roc(c, plumber, "CR-37", "ROC-P")
    _canonical(c, "RCI Systems", a, [a, b])
    assign_all(c)
    groups = presentation_groups(c, [a, b], names={a: "RCI SYSTEMS INC", b: "R C I SYSTEMS INC"})
    assert len(groups) == 1
    assert sorted(next(iter(groups.values()))) == sorted([a, b])
    fire_book = build_lane_book(c, FIRE_BACKFLOW, limit=10)
    assert len(fire_book) == 1
    assert set(fire_book[0]["member_company_ids"]) == {a, b}
    assert fire_book[0]["account_priority_score"] == 88.0
    plumbing_book = build_lane_book(c, PLUMBING_CORE, limit=25)
    names = {r["canonical_name"].upper() for r in plumbing_book}
    assert "KERNS PLUMBING" in names
    assert not any("RCI" in n or "R C I" in n for n in names)
    stats = collapse_stats(c, [a, b, plumber], {a: "RCI SYSTEMS INC", b: "R C I SYSTEMS INC", plumber: "KERNS PLUMBING"})
    assert stats["duplicate_rows_removed"] == 1
    assert stats["presentation_rows"] == 2


def test_not_sales_ready_hidden_from_lane_books():
    c = conn()
    person = _company(c, "Kris Espinoza")
    expeditor = _company(c, "RIDER PERMIT SERVICE")
    plumber = _company(c, "KERNS PLUMBING")
    _priority(c, person, identity="recurring_fuel_gas", score=91.0, demand='{"fuel_gas": 18}', n_gas=18, n_plum=0)
    _priority(c, expeditor, identity="recurring_fuel_gas", score=89.0, demand='{"fuel_gas": 25}', n_gas=25, n_plum=0)
    _priority(c, plumber, score=70.0, demand='{"water_heater": 8, "fuel_gas": 4}', n_plum=8, n_gas=4)
    _roc(c, plumber, "CR-37", "ROC-K")
    assign_all(c)
    review = c.execute(
        "SELECT lane_key, presentable FROM company_sales_lanes WHERE company_id IN (?,?)",
        (person, expeditor),
    ).fetchall()
    assert review
    assert all(r["lane_key"] == IDENTITY_REVIEW and r["presentable"] == 0 for r in review)
    plumbing = build_lane_book(c, PLUMBING_CORE, limit=25)
    fuel = build_lane_book(c, FUEL_GAS_PROPANE, limit=10)
    names = {r["canonical_name"] for r in plumbing + fuel}
    assert "Kris Espinoza" not in names
    assert "RIDER PERMIT SERVICE" not in names
    assert any("KERNS" in n.upper() for n in names)


def test_scores_and_crm_unchanged():
    c = conn()
    cid = _company(c, "KERNS PLUMBING")
    _priority(c, cid, score=76.5, demand='{"water_heater": 8}', n_plum=8)
    _roc(c, cid, "CR-37", "ROC-K")
    _project(c, cid, score=74.2)
    before_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    before_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    before_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority"))
    before_fk = list(c.execute("SELECT id, contractor_company_id FROM permits"))
    crm_n = c.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"]
    assign_all(c)
    persist_books(c, {PLUMBING_CORE: build_lane_book(c, PLUMBING_CORE, limit=25)})
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
    assert callable(merge_companies)
    assert c.execute(
        "SELECT COUNT(*) n FROM companies WHERE lifecycle_state='merged'"
    ).fetchone()["n"] == 0


def test_internal_serializer_hides_banned_fields():
    row = {
        "canonical_name": "KERNS PLUMBING",
        "account_priority_score": 76.5,
        "sales_why_now": "4 fuel-gas permits in 90 days; verified plumbing contractor.",
        "likely_buy": "Gas piping • Water-heater materials",
        "relevant_90d": 4,
        "historical_relevant": 80,
        "contact_name": "Office",
        "contact_role": None,
        "phone": "480-555-0100",
        "email": None,
        "strongest_project": "2026-09-01 | Phoenix | water heater",
        "roc_status_safe": "Licensed plumbing contractor (identity confirmed)",
        "source_family": "roc",
        "match_status": "VERIFIED_MATCH",
        "model_version": "sales-lane-v1",
        "fit": HIGH,
    }
    card = serialize_internal_card(row, [PLUMBING_CORE, FUEL_GAS_PROPANE])
    for banned in BANNED_PUBLIC_FIELDS:
        assert banned not in card
    assert card["priority"] == 76.5
    assert "roc" not in str(card.get("identity_summary") or "").lower()
    detail = serialize_internal_detail(row, lanes=[PLUMBING_CORE], crm=None)
    assert detail["crm_status"] is None
    assert detail["sales_notes"] is None
    for banned in BANNED_PUBLIC_FIELDS:
        assert banned not in detail
    company = serializers.serialize_company(
        {"id": 1, "display_name": "KERNS PLUMBING", "lane_key": PLUMBING_CORE, "fit": HIGH}
    )
    assert "lane_key" not in company
    assert "fit" not in company
    src = inspect.getsource(crm_service.opportunities)
    assert "sales_lanes" not in src
    assert "company_sales_lanes" not in src
    assert "account_priority_score" not in src
    safe = identity_safe("VERIFIED", ["CR-37"], [PLUMBING_CORE])
    assert "CR-37" not in safe
    assert "roc" not in safe.lower()
    assert "plumbing" in safe.lower()


def test_landscape_gas_not_in_fuel_book():
    recs = classify_features({
        "display_name": "CREATIVE ENVIRONMENTS",
        "trade_identity": "recurring_fuel_gas",
        "demand_categories": '{"fuel_gas": 12}',
        "n_gas": 12,
    })
    fuel = next(r for r in recs if r["lane_key"] == FUEL_GAS_PROPANE)
    assert fuel["fit"] == LOW
    assert presentable_in_lane(recs, SPECIALTY_OTHER)
    assert not presentable_in_lane(recs, PLUMBING_CORE)
    assert not presentable_in_lane(recs, FUEL_GAS_PROPANE)
