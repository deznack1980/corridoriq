"""Phase 4D entity resolution + contactability. Ranking tables stay untouched."""

from __future__ import annotations

import csv
import inspect
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.company_resolution.merge import merge_companies
from pipeline.config.settings import SCHEMA_PATH
from pipeline.contactability.coverage import account_contact_summary, coverage_for
from pipeline.contactability.import_research import import_research_csvs
from pipeline.contactability.normalize import INFERRED_UNVERIFIED, VERIFIED, normalize_email, normalize_phone
from pipeline.contactability.official import apply_official_public_contacts
from pipeline.contactability.roles import ESTIMATING, OWNER_PRINCIPAL, UNKNOWN, classify_decision_maker
from pipeline.contactability.store import upsert_channel
from pipeline.contractor_intel.enrichment import seed_enrichment_registry
from pipeline.crm import serializers
from pipeline.crm import service as crm_service
from pipeline.entity.canonical import run_entity_resolution
from pipeline.entity.classify import SAME_HIGH, SAME_PROB, NEEDS, classify_pair, review_duplicate_candidates
from pipeline.entity.names import compact_company_name
from pipeline.entity.refine import refine_company, refine_known_ambiguities
from pipeline.relevance.classify import seed_relevance_profiles
from pipeline.roc.normalize import normalize_company_name

import pipeline.entity.canonical as canonical_mod
import pipeline.entity.refine as refine_mod
import pipeline.contactability.store as store_mod
import pipeline.contactability.import_research as import_mod
import pipeline.contactability.official as official_mod


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


def _priority(c, company_id, identity="plumbing_specialist", score=80.0):
    now = _now()
    c.execute(
        """
        INSERT INTO company_customer_priority (
            profile_key, company_id, account_priority_score, trade_identity,
            trade_identity_score, activity_score, recency_score,
            demand_quality_score, project_quality_score, confidence_score,
            identity_basis, why_now, primary_demand_category, model_version, generated_at
        ) VALUES ('plumbing_supply', ?, ?, ?, 80, 50, 60, 55, 50, 70,
                  'permit_behavior', 'test why now', 'plumbing_service', 'account-priority-v1', ?)
        """,
        (company_id, score, identity, now),
    )
    c.commit()


def _project(c, company_id, score=77):
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
        INSERT INTO projects (permit_id, jurisdiction, project_category, opportunity_score, analyzed_at)
        VALUES (?, 'phoenix_az', 'Plumbing', ?, ?)
        """,
        (pid, score, now),
    )
    c.execute(
        """
        INSERT INTO project_customer_relevance (
            project_id, profile_key, relevance_score, demand_score,
            contractor_fit_score, catalog_scale_score, timing_score,
            model_version, created_at, updated_at
        ) VALUES (?, 'plumbing_supply', 80, 80, 80, 70, 70, 'relevance-plumbing-v1', ?, ?)
        """,
        (pid, now, now),
    )
    c.commit()
    return pid


def test_compact_names_and_duplicate_classes():
    assert compact_company_name("R C I SYSTEMS INC") == compact_company_name("RCI SYSTEMS, INC.")
    assert compact_company_name("GAS PIPING INC (2)") == compact_company_name("Gas Piping Inc")
    assert compact_company_name("KERNS PLUMBING L L C") == compact_company_name("KERNS PLUMBING")
    assert compact_company_name("R P GAS PIPING LLC") == compact_company_name("RP GAS PIPING LLC")
    cls, reasons = classify_pair(
        "ARKAD BUILDERS",
        "A B DAVIS BUILDERS",
        roc_legal="A B Davis Builders LLC",
        roc_dba="A B Davis Builders",
    )
    assert cls == SAME_HIGH
    assert "dba" in " ".join(reasons) or "shared_license_plus_dba" in reasons
    cls2, _ = classify_pair("The Whiting Turner Contracting Company", "WHITING TURNER CONTRACTING C")
    assert cls2 in {SAME_HIGH, SAME_PROB}
    cls3, _ = classify_pair("Anasazi Pools and Spas", "Unrelated Roofing", roc_legal="Hammer Homes")
    assert cls3 == NEEDS


def test_canonical_links_are_non_destructive():
    c = conn()
    a = _company(c, "ARKAD BUILDERS")
    b = _company(c, "A B DAVIS BUILDERS")
    now = _now()
    c.execute(
        """
        INSERT INTO roc_licenses (
            source_record_key, retrieved_at, normalized_license_number, raw_business_name,
            normalized_business_name, raw_dba, normalized_dba, normalized_class, normalized_status,
            created_at, updated_at
        ) VALUES ('k', ?, '087917', 'A B Davis Builders LLC', 'A B DAVIS BUILDERS',
                  'A B Davis Builders', 'A B DAVIS BUILDERS', 'B-1', 'active', ?, ?)
        """,
        (now, now, now),
    )
    c.execute(
        """
        INSERT INTO roc_duplicate_candidates (
            company_id_a, company_id_b, normalized_license_number, roc_business_name,
            reason, match_confidence, created_at
        ) VALUES (?, ?, '087917', 'A B Davis Builders LLC', 'shared_license', 90, ?)
        """,
        (a, b, now),
    )
    permit_id = _project(c, a)
    before_fk = c.execute("SELECT contractor_company_id FROM permits WHERE id=?", (permit_id,)).fetchone()[0]
    before_merged = c.execute(
        "SELECT COUNT(*) n FROM companies WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
    ).fetchone()["n"]
    before_names = {
        r["id"]: r["display_name"]
        for r in c.execute("SELECT id, display_name FROM companies")
    }
    stats = run_entity_resolution(c)
    assert stats["review"]["pairs"] == 1
    links = list(c.execute("SELECT * FROM company_entity_links"))
    assert len(links) >= 2
    assert {int(r["raw_company_id"]) for r in links} >= {a, b}
    after_fk = c.execute("SELECT contractor_company_id FROM permits WHERE id=?", (permit_id,)).fetchone()[0]
    assert after_fk == before_fk == a
    after_merged = c.execute(
        "SELECT COUNT(*) n FROM companies WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
    ).fetchone()["n"]
    assert after_merged == before_merged == 0
    after_names = {
        r["id"]: r["display_name"]
        for r in c.execute("SELECT id, display_name FROM companies")
    }
    assert after_names == before_names
    aliases = list(c.execute("SELECT alias_name FROM company_aliases WHERE company_id IN (?,?)", (a, b)))
    assert aliases


def test_no_merge_helpers_in_phase4d_modules():
    for mod in (canonical_mod, refine_mod, store_mod, import_mod, official_mod):
        src = inspect.getsource(mod)
        assert "merge_companies" not in src
        assert "UPDATE projects SET" not in src
        assert "opportunity_score" not in src or "never" in src.lower()
        assert "account_priority_score" not in src or "never" in src.lower() or "SELECT" in src
        assert "crm_company_relationships" not in src or "COUNT" in src


def test_contact_normalization_provenance_and_inferred():
    assert normalize_phone("(602) 344-9027") == "6023449027"
    assert normalize_email("Info@ParkerAndSons.com") == "info@parkerandsons.com"
    assert classify_decision_maker("President/CEO") == OWNER_PRINCIPAL
    assert classify_decision_maker("Estimator") == ESTIMATING
    assert classify_decision_maker("Qualifying party") == UNKNOWN
    assert classify_decision_maker(None) == UNKNOWN
    c = conn()
    cid = _company(c, "PARKER & SONS, INC")
    assert upsert_channel(
        c,
        company_id=cid,
        contact_type="business_email",
        contact_value="estimating@parkerandsons.com",
        source_family="inferred_pattern",
        verification_status=INFERRED_UNVERIFIED,
        notes="INFERRED_UNVERIFIED",
    )
    assert upsert_channel(
        c,
        company_id=cid,
        contact_type="business_phone",
        contact_value="602-344-9027",
        source_family="official_website",
        verification_status=VERIFIED,
        source_reference="https://www.parkerandsons.com/",
    )
    c.commit()
    row = c.execute(
        "SELECT verification_status, source_family, source_reference FROM company_contact_channels "
        "WHERE contact_type='business_email'"
    ).fetchone()
    assert row["verification_status"] == INFERRED_UNVERIFIED
    assert row["source_family"] == "inferred_pattern"
    phone = c.execute(
        "SELECT verification_status FROM company_contact_channels WHERE contact_type='business_phone'"
    ).fetchone()
    assert phone["verification_status"] == VERIFIED
    summary = account_contact_summary(c, cid)
    assert summary["has_phone"] is True
    assert summary["has_email"] is False
    assert summary["contact_confidence"] == "VERIFIED"


def test_idempotent_enrichment_and_no_crm_bulk():
    c = conn()
    cid = _company(c, "RP GAS PIPING LLC")
    _priority(c, cid, "fuel_gas_specialist", 83.6)
    first = apply_official_public_contacts(c, [cid])
    second = apply_official_public_contacts(c, [cid])
    assert first["channels_written"] >= 1
    assert second["channels_written"] >= 1
    n = c.execute(
        "SELECT COUNT(*) n FROM company_contact_channels WHERE company_id=?", (cid,)
    ).fetchone()["n"]
    n2 = c.execute(
        "SELECT COUNT(*) n FROM company_contact_channels WHERE company_id=?", (cid,)
    ).fetchone()["n"]
    assert n == n2
    assert c.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"] == 0
    flagged = c.execute(
        "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='contact'"
    ).fetchone()
    assert int(flagged[0]) == 0


def test_research_import_and_scores_unchanged(tmp_path: Path):
    c = conn()
    cid = _company(c, "KERNS PLUMBING", city="Mesa")
    _priority(c, cid, "plumbing_specialist", 76.5)
    pid = _project(c, cid, 81)
    csv_path = tmp_path / "corridoriq_contact_enrichment_test.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "company_id",
                "research_phone",
                "research_email",
                "research_website",
                "research_address",
                "primary_contact_name",
                "primary_contact_title",
                "research_status",
                "research_confidence",
                "researched_at",
                "other_useful_information",
            ],
        )
        w.writeheader()
        w.writerow(
            {
                "company_id": str(cid),
                "research_phone": "(480) 264-3990",
                "research_email": "guess@kernsplumbing.com",
                "research_website": "https://kernsplumbing.com",
                "research_address": "Mesa AZ",
                "primary_contact_name": "Pat Kerns",
                "primary_contact_title": "Owner",
                "research_status": "Found",
                "research_confidence": "High",
                "researched_at": "2026-08-04",
                "other_useful_information": "inferred email pattern",
            }
        )
    before_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    before_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    before_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority"))
    before_fk = list(c.execute("SELECT id, contractor_company_id FROM permits"))
    import_research_csvs(c, [csv_path])
    import_research_csvs(c, [csv_path])
    email = c.execute(
        "SELECT verification_status FROM company_contact_channels WHERE contact_type='business_email'"
    ).fetchone()
    assert email["verification_status"] == INFERRED_UNVERIFIED
    owner = c.execute(
        "SELECT decision_maker_class, original_title FROM company_contact_channels "
        "WHERE contact_type='named_contact'"
    ).fetchone()
    assert owner["decision_maker_class"] == OWNER_PRINCIPAL
    assert owner["original_title"] == "Owner"
    after_opp = list(c.execute("SELECT id, opportunity_score FROM projects ORDER BY id"))
    after_rel = list(c.execute("SELECT project_id, relevance_score FROM project_customer_relevance"))
    after_pri = list(c.execute("SELECT company_id, account_priority_score FROM company_customer_priority"))
    after_fk = list(c.execute("SELECT id, contractor_company_id FROM permits"))
    assert before_opp == after_opp
    assert before_rel == after_rel
    assert before_pri == after_pri
    assert before_fk == after_fk
    cov = coverage_for(c, [{"company_id": cid, "account_priority_score": 76.5, "rank": 1,
                            "display_name": "KERNS PLUMBING", "trade_identity": "plumbing_specialist",
                            "primary_demand_category": "plumbing_service", "why_now": "x",
                            "relevant_90d": 4}])
    assert cov["counts"]["has_phone"] == 1
    company = serializers.serialize_company({
        "id": cid, "display_name": "KERNS", "source_family": "roc",
        "canonical_company_id": 9, "match_status": "VERIFIED_MATCH",
        "account_priority_score": 90, "contact_channels": [],
    })
    for banned in ("source_family", "canonical_company_id", "match_status",
                   "account_priority_score", "contact_channels"):
        assert banned not in company
    src = inspect.getsource(crm_service.opportunities)
    assert "account_priority_score" not in src
    assert "company_contact_channels" not in inspect.getsource(crm_service)


def test_refine_does_not_apply_fuzzy_verified_or_write_matches():
    c = conn()
    cid = _company(c, "Arizona Propane", city="Scottsdale")
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
    rec = refine_company(c, cid)
    assert rec["recommended"] in {"POSSIBLE_MATCH", "HIGH_CONFIDENCE_MATCH"}
    assert rec["recommended"] != "VERIFIED_MATCH" or "compact_name_plus_contact_zip" in rec["evidence"]
    refine_known_ambiguities(c, [cid])
    applied = c.execute("SELECT applied FROM entity_match_overrides WHERE company_id=?", (cid,)).fetchone()
    assert int(applied["applied"]) == 0
    status = c.execute("SELECT match_status FROM roc_company_matches WHERE company_id=?", (cid,)).fetchone()
    assert status["match_status"] == "POSSIBLE_MATCH"


def test_canonical_peer_share_does_not_merge():
    c = conn()
    a = _company(c, "R C I SYSTEMS INC")
    b = _company(c, "RCI SYSTEMS, INC.")
    run_entity_resolution(c)
    upsert_channel(
        c,
        company_id=b,
        contact_type="business_phone",
        contact_value="480-894-8711",
        source_family="prior_research",
        verification_status=VERIFIED,
    )
    c.commit()
    from pipeline.contactability.store import share_channels_across_canonical
    share_channels_across_canonical(c)
    peer = c.execute(
        "SELECT contact_value, source_family, verification_status FROM company_contact_channels "
        "WHERE company_id=? AND contact_type='business_phone'",
        (a,),
    ).fetchone()
    assert peer["contact_value"]
    assert peer["source_family"] == "canonical_peer"
    assert peer["verification_status"] == VERIFIED
    assert c.execute(
        "SELECT COUNT(*) n FROM companies WHERE lifecycle_state='merged'"
    ).fetchone()["n"] == 0
    assert callable(merge_companies)
    src = inspect.getsource(canonical_mod.run_entity_resolution)
    assert "merge_companies" not in src
