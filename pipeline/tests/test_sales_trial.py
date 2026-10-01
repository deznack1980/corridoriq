"""Phase 4G plumbing-core contact enrichment + sales trial. Ranking tables stay untouched."""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timezone

from pipeline.company_resolution.merge import merge_companies
from pipeline.config.settings import SCHEMA_PATH, SALES_LANE_VERSION, SALES_TRIAL_VERSION
from pipeline.contactability.coverage import account_contact_summary
from pipeline.contactability.normalize import CANDIDATE, INFERRED_UNVERIFIED, VERIFIED
from pipeline.contactability.official import apply_official_public_contacts
from pipeline.contactability.roles import ESTIMATING, OFFICE_ADMIN, OWNER_PRINCIPAL, classify_decision_maker
from pipeline.contactability.store import share_channels_across_canonical, upsert_channel
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.crm import serializers
from pipeline.crm import service as crm_service
from pipeline.entity.canonical import link_presentation_pair
from pipeline.relevance.classify import seed_relevance_profiles
from pipeline.roc.normalize import normalize_company_name
from pipeline.sales_lanes.lanes import FIRE_BACKFLOW, PLUMBING_CORE
from pipeline.sales_lanes.quality import CLEARLY_BELONGS, PROBABLY_WRONG
from pipeline.sales_lanes.store import seed_lanes
from pipeline.sales_trial.callability import (
    CALL_NOW,
    CALL_WITH_CAUTION,
    NO_ACTIONABLE_CONTACT,
    classify_callability,
)
from pipeline.sales_trial.outcomes import CALL_OUTCOMES, schema_spec
from pipeline.sales_trial.sheet import BANNED_PUBLIC_FIELDS, serialize_call_sheet_card
from pipeline.sales_trial.snapshot import freeze_plumbing_core, load_plumbing_book
from pipeline.sales_trial import __main__ as trial_main


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
    seed_lanes(c)
    return c


def _company(c, name):
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


def _priority(c, company_id, score=80.0, n180=4, **extra):
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
        ) VALUES ('plumbing_supply', ?, ?, 'plumbing_specialist', 80, 50, 60, 55, 50, 70,
                  ?, ?, ?, 0, 4, 10,
                  'permit_behavior', 'test why now',
                  'plumbing_service', ?, 'account-priority-v1', ?)
        """,
        (
            company_id,
            score,
            extra.get("n30", 1),
            extra.get("n90", 2),
            n180,
            extra.get("demand", '{"plumbing_service": 4}'),
            now,
        ),
    )
    c.commit()


def _lane(c, company_id, lane=PLUMBING_CORE, fit="HIGH"):
    c.execute(
        """
        INSERT INTO company_sales_lanes (
            company_id, lane_key, fit, presentable, model_version, created_at
        ) VALUES (?,?,?,1,?,?)
        """,
        (company_id, lane, fit, SALES_LANE_VERSION, _now()),
    )
    c.commit()


def _book(c, rank, company_id, name, *, score=80.0, identity="VERIFIED", quality=CLEARLY_BELONGS, n180=4, members=None):
    c.execute(
        """
        INSERT INTO sales_lane_books (
            lane_key, presentation_rank, canonical_key, canonical_name,
            primary_company_id, member_company_ids, account_priority_score,
            trade_identity, identity_status, roc_status_safe,
            relevant_30d, relevant_90d, relevant_180d, historical_relevant,
            primary_demand, secondary_demand, likely_buy, sales_why_now,
            quality_label, model_version, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            PLUMBING_CORE, rank, name.upper(), name, company_id,
            json.dumps(members or [company_id]), score, "plumbing_specialist",
            identity, "Licensed plumbing contractor",
            1, 2, n180, 10, "plumbing_service", "[]", "Plumbing fixtures",
            "Recent relevant plumbing work.", quality, SALES_LANE_VERSION, _now(),
        ),
    )
    c.commit()


def test_official_source_precedes_directory_candidate():
    c = conn()
    cid = _company(c, "Signature Plumbing AZ LLC")
    upsert_channel(
        c,
        company_id=cid,
        contact_type="business_phone",
        contact_value="(480) 555-0000",
        source_family="public_directory",
        verification_status=CANDIDATE,
        source_reference="https://example.directory/signature",
    )
    c.commit()
    apply_official_public_contacts(c, [cid])
    rows = list(
        c.execute(
            """
            SELECT verification_status, source_family, source_reference
            FROM company_contact_channels
            WHERE company_id=? AND contact_type='business_phone'
            ORDER BY verification_status DESC
            """,
            (cid,),
        )
    )
    assert any(r["verification_status"] == VERIFIED and r["source_family"] == "official_website" for r in rows)
    assert any(r["verification_status"] == CANDIDATE and r["source_family"] == "public_directory" for r in rows)
    refused = upsert_channel(
        c,
        company_id=cid,
        contact_type="business_phone",
        contact_value="(480) 862-5549",
        source_family="official_website",
        verification_status=CANDIDATE,
        source_reference="https://weaker.example/",
    )
    assert refused is False
    official = c.execute(
        """
        SELECT verification_status, source_reference FROM company_contact_channels
        WHERE company_id=? AND contact_type='business_phone' AND source_family='official_website'
        """,
        (cid,),
    ).fetchone()
    assert official["verification_status"] == VERIFIED
    assert "signature-az.com" in (official["source_reference"] or "")


def test_inferred_email_remains_unverified():
    c = conn()
    cid = _company(c, "PARKER & SONS, INC")
    upsert_channel(
        c,
        company_id=cid,
        contact_type="business_email",
        contact_value="info@parkerandsons.com",
        source_family="inferred_pattern",
        verification_status=INFERRED_UNVERIFIED,
    )
    c.commit()
    apply_official_public_contacts(c, [cid])
    email = c.execute(
        "SELECT verification_status FROM company_contact_channels WHERE contact_type='business_email'"
    ).fetchone()
    assert email["verification_status"] == INFERRED_UNVERIFIED
    summary = account_contact_summary(c, cid)
    assert summary["has_email"] is False
    assert summary["has_phone"] is True
    assert summary["contact_confidence"] == VERIFIED


def test_decision_maker_roles_and_original_title():
    assert classify_decision_maker("President") == OWNER_PRINCIPAL
    assert classify_decision_maker("Estimator") == ESTIMATING
    assert classify_decision_maker("Office Administrator") == OFFICE_ADMIN
    assert classify_decision_maker("Qualifying party") != OWNER_PRINCIPAL
    c = conn()
    cid = _company(c, "TEK STAR PLUMBING AND MECHANICAL")
    apply_official_public_contacts(c, [cid])
    owner = c.execute(
        """
        SELECT decision_maker_class, original_title FROM company_contact_channels
        WHERE contact_name='Vince Velonis'
        """
    ).fetchone()
    assert owner["decision_maker_class"] == OWNER_PRINCIPAL
    assert owner["original_title"] == "President"
    admin = c.execute(
        """
        SELECT decision_maker_class, original_title FROM company_contact_channels
        WHERE contact_name='Victoria Nogales'
        """
    ).fetchone()
    assert admin["decision_maker_class"] == OFFICE_ADMIN
    assert admin["original_title"] == "Office Administrator"
    summary = account_contact_summary(c, cid)
    assert summary["has_owner"] is True
    assert summary["has_named"] is True


def test_arizona_delta_presentation_collapse_does_not_merge():
    c = conn()
    a = _company(c, "ARIZONA DELTA MECHANICAL INC")
    b = _company(c, "AZ Delta Mechanical")
    _priority(c, a, 58.0)
    _priority(c, b, 57.8)
    upsert_channel(
        c,
        company_id=a,
        contact_type="business_phone",
        contact_value="480-898-0007",
        source_family="public_licensing",
        verification_status=CANDIDATE,
    )
    c.commit()
    before_merged = c.execute(
        "SELECT COUNT(*) n FROM companies WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
    ).fetchone()["n"]
    link = link_presentation_pair(
        c,
        company_ids=[a, b],
        canonical_name="ARIZONA DELTA MECHANICAL INC",
        evidence={"do_not_merge": True},
    )
    share_channels_across_canonical(c)
    peer = c.execute(
        """
        SELECT source_family, verification_status FROM company_contact_channels
        WHERE company_id=? AND contact_type='business_phone'
        """,
        (b,),
    ).fetchone()
    assert peer["source_family"] == "canonical_peer"
    assert peer["verification_status"] == CANDIDATE
    assert link["merged_rows"] == 0
    assert c.execute(
        "SELECT COUNT(*) n FROM companies WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
    ).fetchone()["n"] == before_merged
    assert c.execute("SELECT COUNT(*) n FROM companies").fetchone()["n"] == 2
    assert callable(merge_companies)


def test_call_now_and_no_actionable_contact():
    ready = classify_callability(
        {"identity_status": "VERIFIED", "quality_label": CLEARLY_BELONGS, "relevant_180d": 3},
        {"actionable": True, "contact_confidence": VERIFIED, "has_phone": True, "has_email": False},
    )
    assert ready[0] == CALL_NOW
    none = classify_callability(
        {"identity_status": "HIGH_CONFIDENCE", "quality_label": CLEARLY_BELONGS, "relevant_180d": 4},
        {"actionable": False, "contact_confidence": "none"},
    )
    assert none[0] == NO_ACTIONABLE_CONTACT
    caution = classify_callability(
        {"identity_status": "UNRESOLVED", "quality_label": CLEARLY_BELONGS, "relevant_180d": 4},
        {"actionable": True, "contact_confidence": CANDIDATE, "has_phone": True},
    )
    assert caution[0] == CALL_WITH_CAUTION
    lane = classify_callability(
        {"identity_status": "HIGH_CONFIDENCE", "quality_label": PROBABLY_WRONG, "relevant_180d": 8},
        {"actionable": True, "contact_confidence": VERIFIED, "has_phone": True, "has_email": True},
    )
    assert lane[0] == CALL_WITH_CAUTION


def test_phase4g_run_preserves_scores_lanes_and_membership():
    c = conn()
    kerns = _company(c, "Signature Plumbing AZ LLC")
    poolside = _company(c, "poolside plumbing inc")
    arizona = _company(c, "ARIZONA DELTA MECHANICAL INC")
    az = _company(c, "AZ Delta Mechanical")
    abc = _company(c, "ABC WATER WORKS INC")
    for cid, score in ((kerns, 76.5), (poolside, 67.5), (arizona, 58.0), (az, 57.8), (abc, 73.4)):
        _priority(c, cid, score)
        _lane(c, cid)
    _lane(c, abc, FIRE_BACKFLOW, "MEDIUM")
    _book(c, 1, kerns, "Signature Plumbing AZ LLC", score=76.5)
    _book(c, 2, poolside, "poolside plumbing inc", score=67.5)
    _book(c, 3, arizona, "ARIZONA DELTA MECHANICAL INC", score=58.0)
    _book(c, 4, az, "AZ Delta Mechanical", score=57.8, identity="UNRESOLVED")
    _book(c, 5, abc, "ABC WATER WORKS INC", score=73.4, quality=PROBABLY_WRONG)
    before_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority ORDER BY company_id"))
    before_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    before_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    before_lanes = list(c.execute("SELECT company_id, lane_key, fit FROM company_sales_lanes ORDER BY company_id, lane_key"))
    before_book = [(r["presentation_rank"], r["canonical_name"]) for r in load_plumbing_book(c)]
    crm_n = c.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"]
    result = trial_main.run(c)
    frozen = freeze_plumbing_core(c)
    assert [r["canonical_name"] for r in frozen] == [n for _, n in before_book]
    assert result["frozen_n"] == 5
    after_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority ORDER BY company_id"))
    after_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    after_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    after_lanes = list(c.execute("SELECT company_id, lane_key, fit FROM company_sales_lanes ORDER BY company_id, lane_key"))
    after_book = [(r["presentation_rank"], r["canonical_name"]) for r in load_plumbing_book(c)]
    assert before_pri == after_pri
    assert before_opp == after_opp
    assert before_rel == after_rel
    assert before_lanes == after_lanes
    assert before_book == after_book
    assert c.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"] == crm_n
    assert c.execute(
        "SELECT COUNT(*) n FROM companies WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
    ).fetchone()["n"] == 0
    kerns_call = c.execute(
        "SELECT status FROM sales_callability WHERE company_id=?", (kerns,)
    ).fetchone()["status"]
    pool_call = c.execute(
        "SELECT status FROM sales_callability WHERE company_id=?", (poolside,)
    ).fetchone()["status"]
    assert kerns_call == CALL_NOW
    assert pool_call == NO_ACTIONABLE_CONTACT
    abc_note = c.execute(
        "SELECT recommendation FROM sales_presentation_notes WHERE topic='abc_water_works_lane'"
    ).fetchone()
    assert abc_note is not None
    assert "FIRE" in abc_note["recommendation"]
    roc_on = c.execute(
        "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='roc'"
    ).fetchone()
    assert int(roc_on[0]) == 0


def test_customer_serializers_hide_internal_contact_metadata():
    company = serializers.serialize_company(
        {
            "id": 1,
            "display_name": "KERNS",
            "source_family": "official_website",
            "verification_status": VERIFIED,
            "decision_maker_class": OWNER_PRINCIPAL,
            "contact_confidence": VERIFIED,
            "callability": CALL_NOW,
            "canonical_company_id": 9,
            "match_status": "VERIFIED_MATCH",
        }
    )
    for banned in (
        "source_family",
        "verification_status",
        "decision_maker_class",
        "contact_confidence",
        "callability",
        "canonical_company_id",
        "match_status",
    ):
        assert banned not in company
    src = inspect.getsource(crm_service)
    assert "company_contact_channels" not in src
    assert "sales_callability" not in src
    assert "decision_maker_class" not in inspect.getsource(serializers)
    card = serialize_call_sheet_card(
        {
            "canonical_name": "KERNS PLUMBING",
            "account_priority_score": 76.5,
            "callability": CALL_NOW,
            "sales_why_now": "why",
            "likely_buy": "pipe",
            "relevant_30d": 1,
            "relevant_90d": 2,
            "relevant_180d": 4,
            "after": {"phone": "480-555-0100", "contact_name": "Pat"},
        },
        [PLUMBING_CORE],
        None,
    )
    assert card["company"] == "KERNS PLUMBING"
    assert card["callability"] == CALL_NOW
    for banned in ("source_family", "verification_status", "decision_maker_class", "match_status"):
        assert banned not in card
    assert schema_spec()["status"] == "DESIGN_ONLY"
    assert "RIGHT_CONTACT" in CALL_OUTCOMES
    assert "sales_call_outcomes" in schema_spec()["proposed_sql"]


def test_contact_provenance_fields_written():
    c = conn()
    cid = _company(c, "DESERT WATER PLUMBING")
    apply_official_public_contacts(c, [cid])
    row = c.execute(
        """
        SELECT source_family, source_reference, discovered_at, verification_status,
               original_title, decision_maker_class, model_version
        FROM company_contact_channels
        WHERE contact_type='business_phone'
        """
    ).fetchone()
    assert row["source_family"] == "official_website"
    assert "desertwateraz.com" in row["source_reference"]
    assert row["discovered_at"]
    assert row["verification_status"] == VERIFIED
    assert row["model_version"]
    assert SALES_TRIAL_VERSION == "sales-trial-v1"
    assert BANNED_PUBLIC_FIELDS
