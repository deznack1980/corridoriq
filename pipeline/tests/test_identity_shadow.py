"""Identity Evidence Shadow — synthetic production fixtures only (every name
below is fictional). The real production database is never opened here."""

from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

import pytest

from pipeline.company_resolution.normalize import normalize_phone
from pipeline.config.settings import SCHEMA_PATH
from pipeline.identity_shadow import normalize as N
from pipeline.identity_shadow.config import ShadowConfig
from pipeline.identity_shadow.run import run_shadow
from pipeline.identity_shadow.store import (
    ShadowPathError, open_production_readonly, open_shadow, validate_shadow_path)

NOW = "2026-09-30T00:00:00+00:00"
AS_OF = "2026-09-30"
REPO = Path(__file__).resolve().parents[2]


class Prod:
    """Synthetic production database shaped like the real tables."""

    def __init__(self, path: Path):
        self.path = path
        self.c = sqlite3.connect(path)
        self.c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for j in ("phoenix_az", "mesa_az", "tempe_az", "goodyear_az", "peoria_az", "scottsdale_az", "gilbert_az"):
            self.c.execute("INSERT INTO jurisdictions (slug, name, state, status) VALUES (?,?, 'AZ', 'connected')",
                           (j, j.split("_")[0].title()))
        self.batch = self.c.execute(
            "INSERT INTO raw_ingest_batch (source_system, source_entity_type, started_at, status) "
            "VALUES ('synthetic','permit',?, 'succeeded')", (NOW,)).lastrowid
        self.n = 0

    def company(self, name, cid=None):
        return self.c.execute(
            "INSERT INTO companies (id, normalized_name, display_name, created_at, updated_at, source_system) "
            "VALUES (?,?,?,?,?, 'permits')", (cid, N.name_key(name) or name, name, NOW, NOW)).lastrowid

    def permit(self, system, payload, *, gc=None, owner=None, company_id=None, date="2026-09-01", history=()):
        self.n += 1
        num = f"SYN-{self.n:06d}"
        pid = self.c.execute(
            "INSERT INTO permits (jurisdiction, permit_number, general_contractor_name, owner_name, "
            "contractor_company_id, issued_date, first_seen_at, last_updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (system, num, gc, owner, company_id, date, NOW, NOW)).lastrowid
        versions = list(history) + [payload]
        for v, body in enumerate(versions, start=1):
            self.c.execute(
                "INSERT INTO raw_record (batch_id, source_system, source_entity_type, source_record_id, payload_json, "
                "payload_hash, fetched_at, version_number, is_current) VALUES (?,?, 'permit', ?,?,?,?,?,?)",
                (self.batch, system, num, json.dumps(body), hashlib.sha256(json.dumps(body).encode()).hexdigest(),
                 NOW, v, 1 if v == len(versions) else 0))
        return pid

    def roc(self, lic, name, street, city, zip_, *, dba=None, cls="CR-37", cap="plumbing"):
        self.c.execute(
            "INSERT INTO roc_licenses (source_record_key, retrieved_at, raw_license_number, normalized_license_number, "
            "raw_business_name, raw_dba, normalized_class, raw_class_type, normalized_status, qualifying_party, "
            "address_line_1, city, state, postal_code, corridor_capability, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, 'active', 'QP PERSON', ?,?, 'AZ', ?,?,?,?)",
            (f"roc-{lic}", NOW, lic, lic, name, dba, cls, "Specialty Dual", street, city, zip_, cap, NOW, NOW))

    def channel(self, company_id, ctype, value, family="official_website", status="VERIFIED"):
        self.c.execute(
            "INSERT INTO company_contact_channels (company_id, contact_type, contact_value, normalized_value, "
            "source_family, discovered_at, verified_at, verification_status, model_version, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, 'test', ?, ?)",
            (company_id, ctype, value, normalize_phone(value) or value, family, NOW, NOW, status, NOW, NOW))

    def crosswalk(self, company_id, lic, legal):
        for f, v in (("license_number", lic), ("legal_name", legal)):
            self.c.execute("INSERT INTO roc_contact_candidates (company_id, field_name, value, source_family, "
                           "source_date, confidence, verification_status, created_at) "
                           "VALUES (?,?,?, 'roc', '2026-01-01', 90, 'candidate', ?)", (company_id, f, v, NOW))

    def done(self):
        self.c.commit()
        self.c.close()


def build_world(path: Path) -> dict:
    p = Prod(path)
    ids = {}
    # CASE A — same name, different licensed business locations.
    p.roc("300001", "Abacus Plumbing LLC", "100 W Main St", "Phoenix", "85001")
    p.roc("300002", "Abacus Plumbing", "9 E Oak St", "Tucson", "85701")
    p.permit("tempe_az", {"ContractorCompanyName": "ABACUS PLUMBING", "ContractorLicNum": "300001"})
    p.permit("mesa_az", {"contractor_name": "Abacus Plumbing"})   # no location, no license → ambiguous
    # CASE B — formatting variants resolve together when evidence supports it.
    p.roc("300010", "Xylem Pipe LLC", "500 N Central Ave", "Phoenix", "85004")
    p.permit("tempe_az", {"ContractorCompanyName": "XYLEM PIPE", "ContractorLicNum": "300010"})
    p.permit("goodyear_az", {"GenConName": "Xylem Pipe, L.L.C.", "GenConStreetNumber": "500",
                             "GenConStreetDirection": "N", "GenConStreetName": "Central", "GenConStreetType": "Avenue",
                             "GenConZipCode": "85004", "GenConCity": "Phoenix"})
    # CASE C — trade-word difference must not merge on name alone.
    p.roc("300015", "Ocelot Plumbing", "77 S Elm St", "Mesa", "85201")
    p.permit("phoenix_az", {"PROFESS_NAME": "Ocelot Plumbing & Mechanical"})
    p.permit("phoenix_az", {"PROFESS_NAME": "OCELOT PLUMBING"})
    # CASE D — explicit DBA.
    p.permit("scottsdale_az", {"Builder": "Kestrel Pools DBA Sunfish Pools And Landscape"})
    p.permit("scottsdale_az", {"ResponsibleParty": "Sunfish Pools and Landscape"})
    # CASE E — shared registered-agent / office address.
    for lic, nm in (("300050", "Heron Electric"), ("300051", "Falcon Roofing")):
        p.roc(lic, nm, "1 Agent Plaza Ste 100", "Phoenix", "85004", cap="electrical")
    for k, nm in enumerate(("Gannet Pools", "Petrel Solar", "Plover Glass", "Tern Fencing")):
        p.roc(f"30006{k}", nm, "2 Registered Way", "Phoenix", "85012", cap="other")
    # CASE F — shared office phone, different businesses.
    p.permit("tempe_az", {"ContractorCompanyName": "Wren Mechanical", "ContractorPhone": "4805550100"})
    p.permit("goodyear_az", {"GenConName": "Lark Heating", "GenConPhoneAreaCode": "480",
                             "GenConPhoneNumber": "5550100"})
    # CASE G — one business, several license classes.
    for lic, cls in (("300020", "B"), ("300021", "C-37"), ("300022", "R-11")):
        p.roc(lic, "Bison Builders LLC", "88 W Pine Rd", "Chandler", "85224", cls=cls, cap="general_contractor")
    # CASE H — person-name sole proprietor corroborated by ROC; uncorroborated persons held.
    p.roc("300030", "Quentin R Halvorsen", "12 N Alder Dr", "Peoria", "85345")
    p.permit("tempe_az", {"ContractorCompanyName": "Quentin R Halvorsen", "ContractorLicNum": "300030"})
    p.permit("phoenix_az", {"PROFESS_NAME": "Quentin R Halvorsen"})
    p.permit("phoenix_az", {"PROFESS_NAME": "Marisol T Underhill"})
    # Peoria: the City's own phone on every record (must be non-identifying).
    for nm in ("Swift Permit Service", "Ibis Homes", "Crane Design", "Kite Builders", "Puffin Renovation"):
        p.permit("peoria_az", {"Applicant_Contact_Organization": nm, "Applicant_Contact_Name": "Alex Q Doe",
                               "ContactPH": "623-773-7225 Option1"})
    # Repeated evidence from one family: 5 Mesa permits, one contractor.
    for _ in range(5):
        p.permit("mesa_az", {"contractor_name": "Thrush Concrete LLC", "contractor_address": "45 W Birch Ave"})
    # Placeholders.
    p.permit("phoenix_az", {"PROFESS_NAME": "AFP I-0869"})
    p.permit("phoenix_az", {"PROFESS_NAME": "OWNER"})
    # Historical name change on one permit.
    p.permit("phoenix_az", {"PROFESS_NAME": "Magpie Tile Co"}, history=[{"PROFESS_NAME": "Jackdaw Tile Co"}])
    # Would-split: production merged two separately licensed same-name businesses.
    ids["robin"] = p.company("Robin Plumbing", 501)
    p.roc("300040", "Robin Plumbing", "3 E Cedar St", "Phoenix", "85003")
    p.roc("300041", "Robin Plumbing", "8 S Fir Ln", "Tucson", "85705")
    p.permit("tempe_az", {"ContractorCompanyName": "Robin Plumbing", "ContractorLicNum": "300040"},
             gc="Robin Plumbing", company_id=501)
    p.permit("goodyear_az", {"GenConName": "Robin Plumbing", "GenConLicenseNumber": "300041",
                             "GenConStreetNumber": "8", "GenConStreetDirection": "S", "GenConStreetName": "Fir",
                             "GenConStreetType": "Ln", "GenConZipCode": "85705"},
             gc="Robin Plumbing", company_id=501)
    # Would-merge: production kept 'The Finch Group' and 'Finch Group' apart.
    p.company("The Finch Group", 601)
    p.company("Finch Group", 602)
    p.permit("phoenix_az", {"PROFESS_NAME": "The Finch Group"}, gc="The Finch Group", company_id=601)
    p.permit("phoenix_az", {"PROFESS_NAME": "Finch Group"}, gc="Finch Group", company_id=602)
    # Contact channel researched for a production company.
    p.company("Egret Plumbing", 701)
    p.permit("phoenix_az", {"PROFESS_NAME": "Egret Plumbing"}, gc="Egret Plumbing", company_id=701)
    p.channel(701, "business_phone", "(602) 555-0177")
    p.channel(701, "business_address", "40 E Jay St, Phoenix, AZ 85004")
    p.channel(701, "business_email", "office@egretplumbing.example")
    # Crosswalk hint alone never creates a company.
    p.company("Crosswalk Only Co", 801)
    p.crosswalk(801, "399999", "Crosswalk Only Co")
    # ROC-only licensee and a formula-looking name.
    p.roc("300090", "Osprey Backflow Testing", "6 W Gull Rd", "Mesa", "85203", cap="plumbing")
    p.roc("300091", "=HYPERLINK(\"x\")", "7 W Gull Rd", "Mesa", "85203")
    # A jurisdiction with no party fields: activity only.
    p.permit("gilbert_az", {"PermitNumber": "G-1", "ProjectName": "Office"})
    p.done()
    return ids


@pytest.fixture()
def world(tmp_path):
    prod = tmp_path / "prod.db"
    ids = build_world(prod)
    cfg = ShadowConfig(as_of=AS_OF)
    s = run_shadow(prod, tmp_path / "shadow" / "identity_evidence_shadow.db", tmp_path / "out", cfg)
    conn = sqlite3.connect(tmp_path / "shadow" / "identity_evidence_shadow.db")
    conn.row_factory = sqlite3.Row
    yield {"prod": prod, "ids": ids, "summary": s, "s": conn, "tmp": tmp_path, "cfg": cfg}
    conn.close()


def canon_for(s, name_key, family=None):
    q = ("SELECT DISTINCT il.canonical_id FROM source_identities si JOIN identity_links il USING (identity_id) "
         "WHERE si.name_key=?" + (" AND si.family=?" if family else ""))
    return {r[0] for r in s.execute(q, (name_key, family) if family else (name_key,))}


def state(s, cid):
    return s.execute("SELECT confidence_state FROM canonical_companies WHERE canonical_id=?", (cid,)).fetchone()[0]


# --------------------------------------------------------------- normalization
def test_phone_parser_fixes_extension_suffix_bug():
    assert normalize_phone("623-773-7225 Option1") != "6237737225"   # the shared helper's bug
    assert N.phone_key("623-773-7225 Option1") == "6237737225"
    assert N.phone_key("(480) 555-0100 x12") == "4805550100"
    assert N.phone_key("+1 602.555.0177") == "6025550177"
    assert N.phone_key("555-0177") is None
    assert N.phone_from_parts("480", "5550100") == "4805550100"


def test_names_dba_placeholders_kinds():
    assert N.name_key("ABC Plumbing LLC") == N.name_key("ABC PLUMBING") == N.name_key("ABC Plumbing, L.L.C.")
    assert N.name_key("ABC Plumbing") != N.name_key("ABC Plumbing & Mechanical")
    assert N.split_dba("ABC Plumbing DBA ABC Mechanical") == ("ABC Plumbing", "ABC Mechanical")
    assert N.split_dba("ABC Plumbing d/b/a ABC Mechanical")[1] == "ABC Mechanical"
    assert N.split_dba("Smith A.K.A. Smith Services")[1] == "Smith Services"
    assert N.split_dba("Dbaker Construction") == ("Dbaker Construction", None)
    assert N.names_agree({"ABC PLUMBING"}, {"ABC PLUMBING AND MECHANICAL"}) == "related"
    assert N.names_agree({"ABC PLUMBING"}, {"XYZ PLUMBING"}) is None
    for p in ("AFP I-0869", "OWNER", "N/A", "12345"):
        assert N.is_placeholder(p), p
    assert N.entity_kind("Juan Q Rivas") == "PERSON"
    assert N.entity_kind("Chris and Beverly Wolter") == "PERSON"
    assert N.entity_kind("Rivas Plumbing LLC") == "BUSINESS"
    assert N.entity_kind("Puffin Renovation") == "BUSINESS"
    assert N.entity_kind("Shrike Landscaping") == "BUSINESS"
    assert N.street_key("77 East Main Street Suite 200") == "77 E MAIN ST"
    assert N.street_key("P.O. Box 10745") == "PO BOX 10745"
    assert N.street_key("10032 W BELL RD 106") == "10032 W BELL RD"
    assert N.domain_from_url("https://www.Example.com/contact") == "example.com"
    assert N.domain_from_email("x@gmail.com") is None
    assert N.license_key("HTE-0001") is None


# --------------------------------------------------------------- collision cases
def test_case_a_same_name_different_locations_not_merged(world):
    s = world["s"]
    roc = canon_for(s, "ABACUS PLUMBING", "roc")
    assert len(roc) == 2                                     # Phoenix and Tucson stay separate
    tempe = canon_for(s, "ABACUS PLUMBING", "permit:tempe_az")
    assert len(tempe) == 1 and tempe <= roc                  # license joins the Phoenix holder
    assert state(s, next(iter(tempe))) == "VERIFIED"
    mesa = canon_for(s, "ABACUS PLUMBING", "permit:mesa_az")
    assert len(mesa) == 1 and not (mesa & roc)               # name-only, ambiguous → separate
    assert state(s, next(iter(mesa))) == "REVIEW_REQUIRED"
    kinds = {r[0] for r in s.execute("SELECT review_kind FROM resolution_reviews")}
    assert {"AMBIGUOUS_NAME", "SAME_NAME_DIFFERENT_LOCATION"} <= kinds


def test_case_b_variants_resolve_with_evidence(world):
    s = world["s"]
    cids = canon_for(s, "XYLEM PIPE")
    assert len(cids) == 1
    cid = next(iter(cids))
    assert state(s, cid) == "VERIFIED"
    fams = {r[0] for r in s.execute("SELECT family FROM identity_signatures WHERE canonical_id=?", (cid,))}
    assert fams == {"roc", "permit:tempe_az", "permit:goodyear_az"}


def test_case_c_trade_word_difference_not_merged(world):
    s = world["s"]
    plain = canon_for(s, "OCELOT PLUMBING")
    mech = canon_for(s, N.name_key("Ocelot Plumbing & Mechanical"))
    assert plain and mech and not (plain & mech)
    # The plain-name permit attaches (as a candidate) to the single licensed Ocelot.
    assert len(plain) == 1


def test_case_d_dba_is_a_relationship_not_a_company(world):
    s = world["s"]
    rel = s.execute("SELECT * FROM company_name_relationships WHERE legal_name_key=?",
                    (N.name_key("Kestrel Pools"),)).fetchone()
    assert rel and rel["dba_name_key"] == N.name_key("Sunfish Pools And Landscape") and rel["relationship"] == "DBA_OF"
    both = canon_for(s, N.name_key("Kestrel Pools")) | canon_for(s, N.name_key("Sunfish Pools and Landscape"))
    assert len(both) == 1                                    # the DBA did not become a second company


def test_case_e_shared_address_never_merges(world):
    s = world["s"]
    for a, b in (("HERON ELECTRIC", "FALCON ROOFING"), ("GANNET POOLS", "PETREL SOLAR")):
        assert canon_for(s, a) != canon_for(s, b)
    assert s.execute("SELECT shared FROM company_addresses WHERE street_key='2 REGISTERED WAY' LIMIT 1").fetchone()[0] == 1


def test_case_f_shared_phone_different_names_not_merged(world):
    s = world["s"]
    assert canon_for(s, "WREN MECHANICAL") != canon_for(s, "LARK HEATING")
    assert world["summary"]["resolver"].get("collisions:PHONE_NAME", 0) >= 1


def test_case_g_multiple_license_classes_one_business(world):
    s = world["s"]
    cids = canon_for(s, "BISON BUILDERS", "roc")
    assert len(cids) == 1
    lics = s.execute("SELECT COUNT(*) FROM company_licenses WHERE canonical_id=?", (next(iter(cids)),)).fetchone()[0]
    assert lics == 3


def test_case_h_sole_proprietor_and_held_persons(world):
    s = world["s"]
    name = N.name_key("Quentin R Halvorsen")
    licensed = canon_for(s, name, "roc")
    assert len(licensed) == 1
    row = s.execute("SELECT * FROM canonical_companies WHERE canonical_id=?", (next(iter(licensed)),)).fetchone()
    assert row["entity_kind"] == "SOLE_PROPRIETOR" and row["confidence_state"] == "VERIFIED"
    # Person names without corroboration are held, not companies.
    for person in ("Quentin R Halvorsen", "Marisol T Underhill"):
        links = s.execute(
            "SELECT il.canonical_id, il.hold_reason FROM source_identities si JOIN identity_links il USING (identity_id) "
            "WHERE si.name_key=? AND si.family='permit:phoenix_az'", (N.name_key(person),)).fetchall()
        assert links and all(l[0] is None and l[1] == "person_name_uncorroborated" for l in links), person
    assert s.execute("SELECT COUNT(*) FROM resolution_reviews WHERE review_kind='PERSON_MATCHES_LICENSEE'").fetchone()[0] == 1
    contacts = s.execute("SELECT il.hold_reason FROM source_identities si JOIN identity_links il USING (identity_id) "
                         "WHERE si.role='applicant_contact'").fetchall()
    assert contacts and all(c[0] == "contact_person" for c in contacts)


def test_city_phone_is_non_identifying(world):
    s = world["s"]
    assert world["summary"]["resolver"]["shared_phones"] >= 1
    peoria = {next(iter(canon_for(s, N.name_key(n)))) for n in
              ("Swift Permit Service", "Ibis Homes", "Crane Design", "Kite Builders", "Puffin Renovation")}
    assert None not in peoria and len(peoria) == 5     # five businesses, none held, none merged
    assert s.execute("SELECT shared FROM company_phones WHERE phone_key='6237737225' LIMIT 1").fetchone()[0] == 1


def test_block_cap_is_enforced(tmp_path):
    p = Prod(tmp_path / "prod.db")
    for i in range(20):  # same name + phone, different streets → 20 signatures in one phone block
        p.permit("tempe_az", {"ContractorCompanyName": "Grebe Electric", "ContractorPhone": "6025550199",
                              "ContractorAddress1": f"{100 + i} W Grebe St", "ContractorZip": "85281"})
    p.done()
    s = run_shadow(tmp_path / "prod.db", tmp_path / "s.db", None,
                   ShadowConfig(as_of=AS_OF, block_cap=10, shared_value_names=1000))
    assert s["resolver"].get("blocks_capped:PHONE_NAME") == 1
    assert s["resolver"].get("block_max:PHONE_NAME") == 20
    assert "links:PHONE_NAME" not in s["resolver"]


def test_chain_cap_refuses_runaway_unions(tmp_path):
    p = Prod(tmp_path / "prod.db")
    for i in range(6):  # each permit asserts a different DBA, all sharing one address → chain of names
        p.permit("tempe_az", {"ContractorCompanyName": f"Shrike Group DBA Shrike Brand {i}",
                              "ContractorAddress1": "9 W Shrike Ave", "ContractorZip": "85281"})
    p.done()
    s = run_shadow(tmp_path / "prod.db", tmp_path / "s.db", None,
                   ShadowConfig(as_of=AS_OF, max_component_names=4, shared_value_names=1000))
    assert s["resolver"].get("refused_chain_cap", 0) >= 1


# --------------------------------------------------------------- independence, provenance, activity
def test_repeated_family_evidence_counts_once(world):
    s = world["s"]
    cid = next(iter(canon_for(s, N.name_key("Thrush Concrete LLC"))))
    row = s.execute("SELECT * FROM canonical_companies WHERE canonical_id=?", (cid,)).fetchone()
    assert row["identity_count"] == 5 and row["signature_count"] == 1
    assert row["family_count"] == 1 and row["confidence_state"] == "SOURCE_ONLY"


def test_prod_reference_contact_gives_independent_corroboration(world):
    s = world["s"]
    cid = next(iter(canon_for(s, "EGRET PLUMBING", "permit:phoenix_az")))
    assert state(s, cid) == "HIGH_CONFIDENCE"
    assert s.execute("SELECT verified FROM company_phones WHERE canonical_id=?", (cid,)).fetchone()[0] == 1
    assert s.execute("SELECT COUNT(*) FROM company_emails WHERE canonical_id=?", (cid,)).fetchone()[0] == 1


def test_crosswalk_alone_never_creates_a_company(world):
    s = world["s"]
    assert not {c for c in canon_for(s, "CROSSWALK ONLY") if c}
    assert s.execute("SELECT COUNT(*) FROM source_identities WHERE family='roc_crosswalk'").fetchone()[0] == 1


def test_provenance_and_lineage(world):
    s = world["s"]
    cols = {r[1] for r in s.execute("PRAGMA table_info(source_records)")}
    assert "payload_json" not in cols                        # raw payloads are referenced, not copied
    rec = s.execute("SELECT * FROM source_records WHERE family='permit:tempe_az' LIMIT 1").fetchone()
    assert rec["prod_table"] == "raw_record" and rec["prod_row_id"] and rec["payload_hash"]
    ev = s.execute("SELECT * FROM identity_evidence WHERE evidence_type='license' LIMIT 1").fetchone()
    assert ev["family"] and ev["ingested_at"] and ev["normalizer_version"] and ev["state"] == "CURRENT"
    ident = s.execute("SELECT * FROM source_identities LIMIT 1").fetchone()
    assert ident["parser_version"] and ident["first_batch_id"] and ident["original_name"]
    prov = {r[0]: r[2] for r in s.execute("SELECT * FROM source_provenance")}
    assert prov["roc"] == 1 and prov["roc_crosswalk"] == 0 and prov["permit:tempe_az"] == 1
    assert {r[0] for r in s.execute("SELECT usage_terms FROM source_provenance")} == {"UNKNOWN"}


def test_history_is_superseded_not_current(world):
    s = world["s"]
    old = s.execute("SELECT * FROM source_identities WHERE name_key=?", (N.name_key("Jackdaw Tile Co"),)).fetchone()
    assert old["state"] == "SUPERSEDED" and old["prod_permit_id"] is None
    assert {r[0] for r in s.execute("SELECT state FROM identity_evidence WHERE identity_id=?",
                                    (old["identity_id"],))} == {"SUPERSEDED"}


def test_activity_is_separate_from_existence(world):
    s = world["s"]
    osprey = next(iter(canon_for(s, "OSPREY BACKFLOW TESTING")))
    row = s.execute("SELECT * FROM canonical_companies WHERE canonical_id=?", (osprey,)).fetchone()
    assert row["permit_count"] == 0 and row["licensed_without_observed_activity"] == 1
    assert world["summary"]["analysis"]["diff"]["roc_only_entities"] >= 1
    thrush = next(iter(canon_for(s, N.name_key("Thrush Concrete LLC"))))
    assert s.execute("SELECT COUNT(*) FROM company_activity_links WHERE canonical_id=?", (thrush,)).fetchone()[0] == 5
    # Gilbert records have no party fields: activity exists in production but no identity is invented.
    assert s.execute("SELECT COUNT(*) FROM source_identities WHERE family='permit:gilbert_az'").fetchone()[0] == 0
    assert s.execute("SELECT COUNT(*) FROM source_records WHERE family='permit:gilbert_az'").fetchone()[0] == 1
    assert world["summary"]["extraction"]["permits"].get("skipped_placeholder", 0) >= 2


# --------------------------------------------------------------- diff
def test_would_split_and_would_merge(world):
    s = world["s"]
    split = s.execute("SELECT detail_json FROM shadow_diff WHERE diff_kind='WOULD_SPLIT' AND subject='501'").fetchone()
    assert split and json.loads(split[0])["shadow_entities"] == 2
    merges = [json.loads(r[0]) for r in s.execute("SELECT detail_json FROM shadow_diff WHERE diff_kind='WOULD_MERGE'")]
    assert any(set(m["production_companies"]) == {601, 602} for m in merges)
    d = world["summary"]["analysis"]["diff"]
    assert d["production_would_split"] >= 1 and d["shadow_would_merge_entities"] >= 1


# --------------------------------------------------------------- idempotency / reprocessing
TABLES = ("source_records", "source_identities", "identity_evidence", "canonical_companies", "resolution_decisions",
          "resolution_reviews", "identity_links", "company_activity_links", "company_names")


def _counts(path):
    c = sqlite3.connect(path)
    try:
        out = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in TABLES}
        out["ids"] = sorted(r[0] for r in c.execute("SELECT canonical_id FROM canonical_companies"))
        out["states"] = sorted(r[0] for r in c.execute("SELECT confidence_state FROM canonical_companies"))
        return out
    finally:
        c.close()


def test_idempotent_rerun(tmp_path):
    build_world(tmp_path / "prod.db")
    shadow = tmp_path / "s.db"
    cfg = ShadowConfig(as_of=AS_OF)
    run_shadow(tmp_path / "prod.db", shadow, None, cfg)
    first = _counts(shadow)
    run_shadow(tmp_path / "prod.db", shadow, None, cfg)
    assert _counts(shadow) == first
    c = sqlite3.connect(shadow)
    assert c.execute("SELECT COUNT(*) FROM shadow_batches WHERE status='succeeded'").fetchone()[0] == 2
    c.close()


def test_reprocessing_rebuilds_derived_state(tmp_path):
    build_world(tmp_path / "prod.db")
    shadow = tmp_path / "s.db"
    a = run_shadow(tmp_path / "prod.db", shadow, None, ShadowConfig(as_of=AS_OF))
    b = run_shadow(tmp_path / "prod.db", shadow, None, ShadowConfig(as_of=AS_OF, shared_value_names=1000))
    c = sqlite3.connect(shadow)
    assert {r[0] for r in c.execute("SELECT DISTINCT batch_id FROM canonical_companies")} == {b["batch_id"]}
    assert c.execute("SELECT COUNT(*) FROM shadow_batches").fetchone()[0] == 2
    c.close()
    assert a["resolver"]["shared_phones"] >= 1 and b["resolver"]["shared_phones"] == 0


def test_removed_source_records_stop_contributing(tmp_path):
    build_world(tmp_path / "prod.db")
    shadow = tmp_path / "s.db"
    run_shadow(tmp_path / "prod.db", shadow, None, ShadowConfig(as_of=AS_OF))
    c = sqlite3.connect(tmp_path / "prod.db")
    c.execute("DELETE FROM raw_record WHERE payload_json LIKE '%Osprey%'")
    c.execute("DELETE FROM roc_licenses WHERE raw_business_name='Osprey Backflow Testing'")
    c.commit()
    c.close()
    run_shadow(tmp_path / "prod.db", shadow, None, ShadowConfig(as_of=AS_OF))
    s = sqlite3.connect(shadow)
    assert s.execute("SELECT COUNT(*) FROM source_identities WHERE name_key='OSPREY BACKFLOW TESTING'").fetchone()[0] == 1
    assert s.execute("SELECT COUNT(*) FROM company_names WHERE name_key='OSPREY BACKFLOW TESTING'").fetchone()[0] == 0
    s.close()


# --------------------------------------------------------------- reports / safety
def test_reports_are_marked_and_formula_safe(world):
    folder = Path(world["summary"]["report_dir"])
    names = {p.name for p in folder.iterdir()}
    assert {"summary.md", "review_queue.csv", "would_split.csv", "would_merge.csv", "source_contribution.csv",
            "metrics.json", "unrepresented_production.csv"} <= names
    for p in folder.glob("*.csv"):
        rows = list(csv.reader(open(p, encoding="utf-8")))
        assert rows[0][0].startswith("INTERNAL / CONFIDENTIAL")
        for row in rows[1:]:
            for cell in row:
                assert not cell.startswith(("=", "+", "-", "@")), (p.name, cell)
    assert "INTERNAL / CONFIDENTIAL" in (folder / "summary.md").read_text(encoding="utf-8")
    assert "no observed activity in current CorridorIQ permit coverage" in (folder / "summary.md").read_text(encoding="utf-8")
    review = (folder / "review_queue.csv").read_text(encoding="utf-8")
    assert "AMBIGUOUS_NAME" in review and "suggested_reviewer_action" in review


def test_production_is_read_only(world):
    before = hashlib.sha256(world["prod"].read_bytes()).hexdigest()
    conn = open_production_readonly(world["prod"])
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO companies (normalized_name, created_at, updated_at) VALUES ('X','x','x')")
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE evil (x)")
    conn.close()
    run_shadow(world["prod"], world["tmp"] / "s2.db", None, world["cfg"])
    assert hashlib.sha256(world["prod"].read_bytes()).hexdigest() == before


def test_shadow_path_guards(tmp_path):
    prod = tmp_path / "prod.db"
    build_world(prod)
    for bad in (prod, tmp_path / "corridoriq.db", tmp_path / "x.sqlite", Path(str(prod) + "-wal"), "", None):
        with pytest.raises(ShadowPathError):
            validate_shadow_path(bad, prod)
    other = tmp_path / "other.db"
    sqlite3.connect(other).execute("CREATE TABLE notes (x)").connection.commit()
    with pytest.raises(ShadowPathError):
        open_shadow(other, prod)
    looks_prod = tmp_path / "copy.db"
    sqlite3.connect(looks_prod).execute("CREATE TABLE permits (x)").connection.commit()
    with pytest.raises(ShadowPathError):
        open_shadow(looks_prod, prod)
    assert not (tmp_path / "fresh").exists()
    with pytest.raises(ShadowPathError):
        validate_shadow_path(tmp_path / "fresh" / "x.txt", prod)
    assert not (tmp_path / "fresh").exists()           # nothing created before validation passes


def test_no_tenant_data_or_writes_to_production_in_code():
    src = "\n".join(p.read_text(encoding="utf-8") for p in (REPO / "pipeline" / "identity_shadow").glob("*.py"))
    assert "tenancy" not in src and "book_match" not in src and "tenant.db" not in src
    assert "mode=ro" in src and "query_only" in src
    # Every write statement in the package targets shadow tables only.
    shadow_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)",
                                   (REPO / "pipeline" / "identity_shadow" / "schema.py").read_text(encoding="utf-8")))
    for verb_table in re.findall(r"(?:INSERT INTO|(?<!DO )UPDATE|DELETE FROM)\s+(\w+)", src):
        if verb_table in ("{t}", "{table}") or verb_table.startswith("{"):
            continue
        assert verb_table in shadow_tables, verb_table


def test_metrics_and_contribution_present(world):
    a = world["summary"]["analysis"]
    m = a["metrics"]
    for k in ("canonical_companies", "families:1", "families:2+", "with_roc_identity", "with_address",
              "with_phone", "with_email", "with_domain", "with_recent_permit_activity", "with_trade_evidence",
              "orphan_source_identity_rate", "resolution_rate", "review_rate", "conflict_rate",
              "would_merge_rate", "would_split_rate", "new_entities_created", "existing_entities_enriched"):
        assert k in m, k
    assert "roc" in a["source_contribution"] and "permit:tempe_az" in a["source_contribution"]
    assert a["source_contribution"]["roc"]["licenses_contributed"] >= 10


# --------------------------------------------------------------- performance
def test_200k_identity_performance(tmp_path):
    """~200k source identities, modelled on reality: far fewer companies,
    repeated activity, aliases/DBAs, multi-license holders, shared values, noise."""
    path = tmp_path / "perf_prod.db"
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    for j in ("phoenix_az", "mesa_az", "tempe_az", "scottsdale_az"):
        c.execute("INSERT INTO jurisdictions (slug, name, state, status) VALUES (?,?, 'AZ', 'connected')", (j, j))
    b = c.execute("INSERT INTO raw_ingest_batch (source_system, source_entity_type, started_at, status) "
                  "VALUES ('synthetic','permit',?, 'succeeded')", (NOW,)).lastrowid
    companies = 12000
    roc_rows = []
    for i in range(50000):                      # 50k licenses; 1 in 6 is a second class of the same business
        biz = i if i % 6 else i - 1
        roc_rows.append((f"r{i}", NOW, f"{400000 + i:06d}", f"{400000 + i:06d}", f"Perf Trade {biz:05d} LLC",
                         f"Perf Brand {biz:05d}" if biz % 9 == 0 else None, f"{biz % 900 + 1} W Perf St", "Phoenix",
                         f"{85000 + biz % 90:05d}", NOW, NOW))
    c.executemany("INSERT INTO roc_licenses (source_record_key, retrieved_at, raw_license_number, "
                  "normalized_license_number, raw_business_name, raw_dba, address_line_1, city, postal_code, "
                  "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)", roc_rows)
    permits, raws = [], []
    systems = ("phoenix_az", "mesa_az", "tempe_az", "scottsdale_az")
    for i in range(150000):                     # 150k permits → ~150k+ identities over ~12k companies
        biz = (i * 7919) % companies
        sysn = systems[i % 4]
        num = f"P{i:07d}"
        name = f"Perf Trade {biz:05d}" + (" L.L.C." if i % 3 == 0 else "")
        if sysn == "phoenix_az":
            payload = {"PROFESS_NAME": name if i % 50 else "OWNER"}
        elif sysn == "mesa_az":
            payload = {"contractor_name": name, "contractor_address": f"{biz % 900 + 1} W Perf St",
                       "applicant": "Pat Q Applicant" if i % 2 else name}
        elif sysn == "tempe_az":
            payload = {"ContractorCompanyName": name, "ContractorLicNum": f"{400000 + biz:06d}",
                       "ContractorPhone": f"602555{biz % 10000:04d}"}
        else:
            payload = {"Builder": name + (f" DBA Perf Brand {biz:05d}" if biz % 9 == 0 else ""), "Owner": "Lee Owner"}
        permits.append((sysn, num, NOW, NOW, "2026-08-01"))
        raws.append((b, sysn, num, json.dumps(payload), str(i), NOW))
    c.executemany("INSERT INTO permits (jurisdiction, permit_number, first_seen_at, last_updated_at, issued_date) "
                  "VALUES (?,?,?,?,?)", permits)
    c.executemany("INSERT INTO raw_record (batch_id, source_system, source_entity_type, source_record_id, "
                  "payload_json, payload_hash, fetched_at) VALUES (?,?, 'permit', ?,?,?,?)", raws)
    c.commit()
    c.close()
    t0 = time.perf_counter()
    s = run_shadow(path, tmp_path / "perf_shadow.db", tmp_path / "out", ShadowConfig(as_of=AS_OF),
                   measure_memory=False)
    total = time.perf_counter() - t0
    ids = sum(v for k, v in s["resolver"].items() if k == "signatures")
    considered = s["analysis"]["metrics"]["source_identities_considered"]
    assert considered >= 190000, considered
    assert s["analysis"]["metrics"]["canonical_companies"] < 60000      # massive data ≠ massive companies
    assert ids < considered
    assert total < 900, s["timings"]
    print("PERF", json.dumps({"total_s": round(total, 1), "identities": considered, "signatures": ids,
                              "canonical": s["analysis"]["metrics"]["canonical_companies"],
                              "timings": s["timings"]}))


def test_street_only_address_is_not_a_business_location(tmp_path):
    """Mesa publishes a contractor street without city/ZIP. Two such streets for
    one name must not fragment the business; a PO box alone must not block the
    business from attaching to its single licensed location."""
    p = Prod(tmp_path / "prod.db")
    p.roc("310001", "Tanager Electric Inc", "906 S Priest Dr", "Tempe", "85281", cap="electrical")
    p.permit("mesa_az", {"contractor_name": "Tanager Electric Inc", "contractor_address": "PO Box 804"})
    p.permit("mesa_az", {"contractor_name": "Tanager Electric Inc", "contractor_address": "222 N 44th St"})
    p.permit("phoenix_az", {"PROFESS_NAME": "TANAGER ELECTRIC INC"})
    p.done()
    run_shadow(tmp_path / "prod.db", tmp_path / "s.db", None, ShadowConfig(as_of=AS_OF))
    s = sqlite3.connect(tmp_path / "s.db")
    cids = {r[0] for r in s.execute(
        "SELECT DISTINCT il.canonical_id FROM source_identities si JOIN identity_links il USING (identity_id) "
        "WHERE si.name_key='TANAGER ELECTRIC'")}
    s.close()
    assert len(cids) == 1


def test_shared_values_never_count_as_new_evidence(world):
    peoria = world["summary"]["analysis"]["source_contribution"]["permit:peoria_az"]
    assert peoria["phones_contributed"] == 0 and peoria["phones_shared_non_identifying"] == 5
