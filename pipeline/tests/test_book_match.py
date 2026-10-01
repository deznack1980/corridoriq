"""Customer-Book Match: import, matching quality, classification, review,
reports, CLI, privacy, and performance. SYNTHETIC DATA ONLY — every company
and account below is fictional. Shared "intelligence" is a temporary database
built from schema.sql; the production database is never opened.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import sqlite3
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

from pipeline.book_match import normalize as N
from pipeline.book_match.classify import classify_run
from pipeline.book_match.config import BookMatchConfig
from pipeline.book_match.importer import BookImportError, import_book
from pipeline.book_match.intel import IntelIndex, open_readonly
from pipeline.book_match.matcher import HIGH, REVIEW, UNMATCHED, VERIFIED, match_book
from pipeline.book_match.report import escape_cell, generate_reports, unescape_cell
from pipeline.book_match.review import apply_review_csv
from pipeline.book_match.run import load_index, run_book_match
from pipeline.config.settings import SCHEMA_PATH
from pipeline.sales_lanes.store import seed_lanes
from pipeline.tenancy import TenantRegistry

AS_OF = "2026-09-30"
TODAY = date.fromisoformat(AS_OF)
NOW = "2026-09-30T00:00:00+00:00"


def _days_ago(n: int) -> str:
    return (TODAY - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Synthetic shared intelligence
# ---------------------------------------------------------------------------

# key: (display name, city, lanes, recent permits, prior permits)
COMPANIES = {
    "saguaro":   ("Saguaro Pipe Works LLC", "Phoenix", ["PLUMBING_CORE"], 4, 1),
    "mesquite":  ("Mesquite Mechanical Inc", "Mesa", ["HVAC_MECHANICAL"], 0, 3),
    "dsun_plmb": ("Desert Sun Plumbing LLC", "Mesa", ["PLUMBING_CORE"], 2, 2),
    "dsun_pool": ("Desert Sun Pools LLC", "Mesa", ["SPECIALTY_OTHER"], 5, 1),
    "copper_phx": ("Copper Canyon Contractors", "Phoenix", ["PLUMBING_CORE"], 1, 0),
    "copper_tus": ("Copper Canyon Contractors", "Tucson", [], 1, 0),
    "ironwood":  ("Ironwood Fire Protection LLC", "Tempe", ["FIRE_BACKFLOW"], 3, 0),
    "ortega":    ("Juan Carlos Ortega", "Phoenix", ["PLUMBING_CORE"], 1, 0),
    "agave":     ("Agave Plumbing & Drain", "Peoria", ["PLUMBING_CORE"], 2, 0),
    "ocotillo":  ("Ocotillo Water Systems", "Phoenix", ["CIVIL_WET_UTILITY"], 6, 2),
    "paloverde": ("Palo Verde Builders", "Phoenix", ["GENERAL_CONTRACTOR_CM"], 9, 4),
    "cholla":    ("Cholla Plumbing Co", "Phoenix", ["PLUMBING_CORE"], 1, 1),
}
PHONES = {  # key: (phone, verification status)
    "saguaro": ("602-555-0101", "VERIFIED"),
    "mesquite": ("480-555-0102", "VERIFIED"),
    "dsun_pool": ("480-555-0104", "VERIFIED"),
    "agave": ("623-555-0108", "CANDIDATE"),
    "cholla": ("602-555-0111", "VERIFIED"),
}
ADDRESSES = {
    "saguaro": "1201 W Camelback Rd, Phoenix, AZ 85013",
    "mesquite": "77 E Main St Ste 200, Mesa, AZ 85201",
    "dsun_plmb": "455 N Center St, Mesa, AZ 85201",
    "agave": "15 W Elm St, Peoria, AZ 85345",
    "cholla": "300 E Van Buren St, Phoenix, AZ 85004",
}
ROC = {  # key: (license, street, city, zip)
    "saguaro": ("312001", "1201 W Camelback Rd", "Phoenix", "85013"),
    "ironwood": ("312006", "900 S 48th St", "Tempe", "85281"),
}
ALIASES = {"ironwood": "IRONWOOD FIRE"}


def build_intel_db(path: Path, extra_companies: int = 0) -> dict:
    """Temporary shared-intelligence DB with fictional companies. Returns key -> id."""
    c = sqlite3.connect(path)
    c.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) VALUES ('phoenix_az','Phoenix','AZ','connected')")
    seed_lanes(c)  # lane catalog (foreign key target); module used read-only
    ids = {}
    seq = 0

    def add_company(name, city):
        return c.execute(
            "INSERT INTO companies (normalized_name, display_name, legal_name, city, state, lifecycle_state, "
            "created_at, updated_at) VALUES (?,?,?,?, 'AZ','active',?,?)",
            (N.name_key(name), name, name, city, NOW, NOW)).lastrowid

    def add_permits(cid, recent, prior):
        nonlocal seq
        for i in range(recent):
            seq += 1
            c.execute("INSERT INTO permits (jurisdiction, permit_number, contractor_company_id, issued_date, "
                      "first_seen_at, last_updated_at) VALUES ('phoenix_az', ?, ?, ?, ?, ?)",
                      (f"SYN-{seq}", cid, _days_ago(10 + i), NOW, NOW))
        for i in range(prior):
            seq += 1
            c.execute("INSERT INTO permits (jurisdiction, permit_number, contractor_company_id, issued_date, "
                      "first_seen_at, last_updated_at) VALUES ('phoenix_az', ?, ?, ?, ?, ?)",
                      (f"SYN-{seq}", cid, _days_ago(120 + i), NOW, NOW))

    for key, (name, city, lanes, recent, prior) in COMPANIES.items():
        cid = add_company(name, city)
        ids[key] = cid
        add_permits(cid, recent, prior)
        for lane in lanes:
            c.execute("INSERT INTO company_sales_lanes (company_id, lane_key, fit, presentable, model_version, "
                      "created_at) VALUES (?,?, 'HIGH', 1, 'test', ?)", (cid, lane, NOW))
    for key, (phone, status) in PHONES.items():
        c.execute("INSERT INTO company_contact_channels (company_id, contact_type, contact_value, normalized_value, "
                  "source_family, discovered_at, verification_status, model_version, created_at, updated_at) "
                  "VALUES (?, 'business_phone', ?, ?, 'synthetic', ?, ?, 'test', ?, ?)",
                  (ids[key], phone, N.phone_key(phone), NOW, status, NOW, NOW))
    for key, addr in ADDRESSES.items():
        c.execute("INSERT INTO company_contact_channels (company_id, contact_type, contact_value, normalized_value, "
                  "source_family, discovered_at, verification_status, model_version, created_at, updated_at) "
                  "VALUES (?, 'business_address', ?, ?, 'synthetic', ?, 'VERIFIED', 'test', ?, ?)",
                  (ids[key], addr, addr.upper(), NOW, NOW, NOW))
    for key, (lic, street, city, zipc) in ROC.items():
        lid = c.execute(
            "INSERT INTO roc_licenses (source_record_key, retrieved_at, normalized_license_number, "
            "raw_business_name, address_line_1, city, state, postal_code, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?, 'AZ', ?,?,?)",
            (f"roc-{lic}", NOW, lic, COMPANIES[key][0], street, city, zipc, NOW, NOW)).lastrowid
        c.execute("INSERT INTO roc_company_matches (company_id, roc_license_id, normalized_license_number, "
                  "match_status, match_confidence, matching_version, created_at, updated_at) "
                  "VALUES (?,?,?, 'VERIFIED_MATCH', 0.99, 'test', ?, ?)", (ids[key], lid, lic, NOW, NOW))
    for key, alias in ALIASES.items():
        c.execute("INSERT INTO company_aliases (company_id, alias_name, normalized_alias, created_at) "
                  "VALUES (?,?,?,?)", (ids[key], alias, N.name_key(alias), NOW))
    for i in range(extra_companies):  # background noise for performance tests
        cid = add_company(f"Synthetic Trade Company {i:05d} LLC", "Phoenix")
        if i % 5 == 0:
            add_permits(cid, 1, 0)
    c.commit()
    c.close()
    return ids


ACCOUNT_HEADER = ["Account #", "Customer Name", "DBA", "Address", "City", "State", "Zip", "Phone",
                  "ROC License", "Branch", "Rep", "Last Purchase Date"]

BOOK_A = [
    ["A-1001", "SAGUARO PIPE WORKS, L.L.C.", "", "1201 West Camelback Road", "Phoenix", "AZ", "85013",
     "(602) 555-0101", "ROC 312001", "Central", "Rep One", _days_ago(29)],
    ["A-1002", "Mesquite Mechanical", "", "77 East Main Street Suite 200", "Mesa", "AZ", "85201",
     "480.555.0102", "", "East", "Rep Two", _days_ago(199)],
    ["A-1003", "Desert Sun Plumbing", "", "455 North Center Street #3", "Mesa", "AZ", "85201-1234",
     "", "", "East", "Rep Two", _days_ago(628)],
    ["A-1004", "Desert Sun Pool & Spa", "", "", "Mesa", "AZ", "", "", "", "East", "Rep Two", _days_ago(20)],
    ["A-1005", "The Copper Canyon Contractors, Inc.", "", "", "Phoenix", "AZ", "", "", "", "Central", "Rep One",
     _days_ago(45)],
    ["A-1006", "Ironwood Fire Protection", "", "900 S 48th Street", "Tempe", "AZ", "85281", "", "312006",
     "Central", "Rep One", _days_ago(400)],
    ["A-1007", "Juan Carlos Ortega", "", "", "Phoenix", "AZ", "", "", "", "Central", "Rep One", _days_ago(10)],
    ["A-1008", "Cholla Plumbing Co.", "", "", "Phoenix", "AZ", "", "602-555-0199", "", "Central", "Rep One",
     _days_ago(15)],
    ["A-1009", "Big Horn Hydronics", "", "1 Nowhere Rd", "Phoenix", "AZ", "85001", "602-555-0177", "", "Central",
     "Rep One", _days_ago(5)],
    ["A-1010", "Ocotillo Water Systems", "", "", "Phoenix", "AZ", "", "(602) 555-0101", "", "Central",
     "Rep One", _days_ago(60)],
    ["A-1011", "Agave Plumbing and Drain LLC", "", "15 West Elm Street", "Peoria", "AZ", "85345",
     "+1 623 555 0108", "", "West", "Rep Three", ""],
    ["A-1012", "=HYPERLINK(\"http://example.invalid\",\"x\")", "", "", "", "", "", "", "", "", "", ""],
    ["", "Missing Account Id LLC", "", "", "", "", "", "", "", "", "", ""],
    ["A-1013", "", "", "", "", "", "", "", "", "", "", ""],
    ["A-2000", "Duplicate Row One", "", "", "", "", "", "", "", "", "", ""],
    ["A-2000", "Duplicate Row Two", "", "", "", "", "", "", "", "", "", ""],
    ["A-1014", "Bad Date Plumbing", "", "", "", "", "", "", "", "", "", "13/45/2026"],
    ["A-1015", "Future Date Plumbing", "", "", "", "", "", "", "", "", "", "2031-01-01"],
    ["A-1016", "x" * 400, "", "", "", "", "", "", "", "", "", ""],
]

EXPECTED_A = {
    "A-1001": VERIFIED,   # license + phone + address + name
    "A-1002": HIGH,       # exact name + phone (+ suite-insensitive address)
    "A-1003": HIGH,       # exact name + address (unit and ZIP+4 differences)
    "A-1004": UNMATCHED,  # similar-sounding but different business
    "A-1005": REVIEW,     # name shared by two companies, nothing corroborates
    "A-1006": VERIFIED,   # ROC license + name + address
    "A-1007": REVIEW,     # person name only
    "A-1008": REVIEW,     # name matches, verified phone conflicts
    "A-1009": UNMATCHED,  # unknown business
    "A-1010": REVIEW,     # another company's phone; names differ
    "A-1011": HIGH,       # '& vs and' + candidate phone + address
}


def write_csv(path: Path, rows, header=ACCOUNT_HEADER) -> Path:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    return path


def sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.fixture()
def cfg():
    return BookMatchConfig(as_of=AS_OF).validate()


@pytest.fixture()
def world(tmp_path, cfg):
    intel = tmp_path / "intel.db"
    ids = build_intel_db(intel)
    reg = TenantRegistry(tmp_path / "tenant-root")
    reg.create("supplier-a", "Synthetic Supplier A")
    reg.create("supplier-b", "Synthetic Supplier B")
    return {"tmp": tmp_path, "intel": intel, "ids": ids, "reg": reg,
            "a": reg.store(reg.context("supplier-a")), "b": reg.store(reg.context("supplier-b")),
            "index": load_index(intel, cfg)}


def _matches(store, run_id) -> dict:
    with store.connect() as conn:
        return {r["supplier_account_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM account_matches WHERE run_id=?", (run_id,))}


def _classes(store, run_id) -> dict:
    with store.connect() as conn:
        return {r["supplier_account_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM account_classifications WHERE run_id=?", (run_id,))}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

def test_normalized_keys_absorb_formatting_noise():
    assert N.name_key("SAGUARO PIPE WORKS, L.L.C.") == N.name_key("Saguaro Pipe Works LLC")
    assert N.name_key("Agave Plumbing and Drain LLC") == N.name_key("Agave Plumbing & Drain")
    assert N.street_key("77 East Main Street Suite 200") == N.street_key("77 E Main St Ste 200") == "77 E MAIN ST"
    assert N.street_key("455 North Center Street #3") == "455 N CENTER ST"
    assert N.street_key("PO Box 12") is None
    assert N.phone_key("(602) 555-0101") == N.phone_key("602.555.0101") == N.phone_key("+1 602 555 0101")
    assert N.phone_key("555-0101") is None
    assert N.license_key("ROC 312001") == "312001"
    assert N.zip5("85201-1234") == "85201"


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def test_import_accounts_rejects_and_warnings(world):
    src = write_csv(world["tmp"] / "book_a.csv", BOOK_A)
    res = import_book(world["a"], src, today=TODAY)
    assert (res.row_count, res.accepted, res.rejected) == (19, 14, 5)
    with world["a"].connect() as conn:
        issues = [(r["row_number"], r["level"], r["reason"]) for r in conn.execute(
            "SELECT * FROM import_issues ORDER BY row_number, reason")]
        acct = dict(conn.execute("SELECT * FROM book_accounts WHERE supplier_account_id='A-1001'").fetchone())
        norm = dict(conn.execute("SELECT * FROM account_normalized WHERE supplier_account_id='A-1001'").fetchone())
        batch = dict(conn.execute("SELECT * FROM import_batches").fetchone())
    reasons = {(row, reason) for row, _lvl, reason in issues}
    assert (14, "missing_account_id") in reasons
    assert (15, "missing_company_name") in reasons
    assert (16, "duplicate_account_id_in_file") in reasons and (17, "duplicate_account_id_in_file") in reasons
    assert (20, "field_too_long") in reasons
    assert (18, "unparseable_last_purchase_date") in reasons
    assert (19, "future_last_purchase_date") in reasons
    # Source values preserved; normalized keys stored separately.
    assert acct["company_name"] == "SAGUARO PIPE WORKS, L.L.C." and acct["phone"] == "(602) 555-0101"
    assert norm["name_key"] == "SAGUARO PIPE WORKS" and norm["phone_key"] == "6025550101"
    assert batch["source_filename"] == "book_a.csv" and batch["source_sha256"] == sha(src)
    assert batch["tenant_id"] == "supplier-a"


def test_issue_preview_does_not_expose_full_values(world):
    src = write_csv(world["tmp"] / "book.csv", [["A-1", "y" * 400, "", "", "", "", "", "", "", "", "", ""]])
    import_book(world["a"], src, today=TODAY)
    with world["a"].connect() as conn:
        preview = conn.execute("SELECT field_preview FROM import_issues").fetchone()["field_preview"]
    assert "len 400" in preview and "y" * 10 not in preview


def test_reimport_identical_file_is_noop(world):
    src = write_csv(world["tmp"] / "book_a.csv", BOOK_A)
    first = import_book(world["a"], src, today=TODAY)
    second = import_book(world["a"], src, today=TODAY)
    assert second.already_imported and second.batch_id == first.batch_id
    with world["a"].connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 1


def test_reimport_is_a_snapshot(world):
    import_book(world["a"], write_csv(world["tmp"] / "v1.csv", BOOK_A[:3]), today=TODAY)
    changed = [list(BOOK_A[0])]
    changed[0][9] = "North"
    import_book(world["a"], write_csv(world["tmp"] / "v2.csv", changed), today=TODAY)
    with world["a"].connect() as conn:
        rows = {r["supplier_account_id"]: dict(r) for r in conn.execute("SELECT * FROM book_accounts")}
    assert rows["A-1001"]["branch"] == "North" and rows["A-1001"]["in_latest_batch"] == 1
    assert rows["A-1002"]["in_latest_batch"] == 0 and rows["A-1003"]["in_latest_batch"] == 0


def test_failed_import_rolls_back_completely(world, monkeypatch):
    import pipeline.book_match.importer as imp
    calls = {"n": 0}
    real = imp._upsert_account

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("disk full (simulated)")
        return real(*a, **k)

    monkeypatch.setattr(imp, "_upsert_account", flaky)
    with pytest.raises(RuntimeError):
        import_book(world["a"], write_csv(world["tmp"] / "book_a.csv", BOOK_A), today=TODAY)
    with world["a"].connect() as conn:
        for table in ("import_batches", "book_accounts", "account_normalized", "import_issues"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table


@pytest.mark.parametrize("content,expect", [
    (b"Account #,Customer Name\r\nA-1,Caf\xe9 Plumbing\r\n", "UTF-8"),
    (b"Account #,Customer Name\r\nA-1,Acme\x00\r\n", "NUL"),
    (b"Name,Phone\r\nAcme,6025550101\r\n", "missing required"),
    (b"Account #,Customer Name,Name\r\nA-1,Acme,x\r\n", "same field"),
    (b"", "empty"),
    (b'Account #,Customer Name\r\n"A-1,"Acme\r\n', "malformed"),
])
def test_unusable_files_are_rejected_whole(world, content, expect):
    p = world["tmp"] / "bad.csv"
    p.write_bytes(content)
    with pytest.raises(BookImportError, match=expect):
        import_book(world["a"], p, today=TODAY)
    with world["a"].connect() as conn:
        conn.executescript(__import__("pipeline.book_match.schema", fromlist=["x"]).BOOK_SCHEMA)
        assert conn.execute("SELECT COUNT(*) FROM book_accounts").fetchone()[0] == 0


def test_concurrent_imports_serialize_cleanly(world):
    files = [write_csv(world["tmp"] / f"c{i}.csv",
                       [[f"C{i}-{j}", f"Concurrent Co {i} {j}", "", "", "", "", "", "", "", "", "", ""]
                        for j in range(200)]) for i in range(2)]
    errors = []

    def go(f):
        try:
            import_book(world["a"], f, today=TODAY)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=go, args=(f,)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    with world["a"].connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM import_batches").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM book_accounts").fetchone()[0] == 400
        assert conn.execute("SELECT COUNT(*) FROM book_accounts WHERE in_latest_batch=1").fetchone()[0] == 200


# ---------------------------------------------------------------------------
# Matching quality
# ---------------------------------------------------------------------------

@pytest.fixture()
def matched(world, cfg):
    batch = import_book(world["a"], write_csv(world["tmp"] / "book_a.csv", BOOK_A), today=TODAY)
    run_id, counts = match_book(world["a"], world["index"], cfg, batch.batch_id)
    return {**world, "run_id": run_id, "counts": counts, "m": _matches(world["a"], run_id)}


@pytest.mark.parametrize("acct,expected", sorted(EXPECTED_A.items()))
def test_expected_confidence(matched, acct, expected):
    assert matched["m"][acct]["confidence"] == expected, matched["m"][acct]


def test_confident_matches_point_at_the_right_company(matched):
    ids, m = matched["ids"], matched["m"]
    assert m["A-1001"]["company_id"] == ids["saguaro"]
    assert m["A-1002"]["company_id"] == ids["mesquite"]
    assert m["A-1003"]["company_id"] == ids["dsun_plmb"]
    assert m["A-1006"]["company_id"] == ids["ironwood"]
    assert m["A-1011"]["company_id"] == ids["agave"]


def test_dangerous_cases_are_never_confident(matched):
    m = matched["m"]
    for acct in ("A-1004", "A-1005", "A-1007", "A-1008", "A-1010"):
        assert m[acct]["confidence"] in (REVIEW, UNMATCHED), acct
    assert m["A-1004"]["company_id"] is None  # no similar-name guess recorded
    assert "phone" in json.loads(m["A-1008"]["conflicts_json"])[0] or "conflict" in m["A-1008"]["review_reason"]
    assert "2 CorridorIQ companies" in m["A-1005"]["review_reason"]
    assert "person" in m["A-1007"]["review_reason"]
    assert "names differ" in m["A-1010"]["review_reason"]


def test_no_numeric_scores_are_stored(matched):
    for row in matched["m"].values():
        blob = row["evidence_json"] + row["conflicts_json"] + row["candidates_json"]
        assert not any(ch.isdigit() for ch in json.dumps(json.loads(row["evidence_json"])))
        assert "score" not in blob and "similarity" not in blob


def test_name_plus_city_is_review_unless_policy_allows(world, cfg):
    rows = [["X-1", "Mesquite Mechanical", "", "", "Mesa", "AZ", "", "", "", "", "", _days_ago(5)]]
    batch = import_book(world["a"], write_csv(world["tmp"] / "x.csv", rows), today=TODAY)
    run_id, _ = match_book(world["a"], world["index"], cfg, batch.batch_id)
    assert _matches(world["a"], run_id)["X-1"]["confidence"] == REVIEW
    loose = BookMatchConfig(as_of=AS_OF, allow_name_city_high_confidence=True).validate()
    run2, _ = match_book(world["a"], world["index"], loose, batch.batch_id)
    assert _matches(world["a"], run2)["X-1"]["confidence"] == HIGH
    # Duplicate names never become confident even under the looser policy.
    rows = [["X-2", "Copper Canyon Contractors", "", "", "Phoenix", "AZ", "", "", "", "", "", _days_ago(5)]]
    b2 = import_book(world["a"], write_csv(world["tmp"] / "y.csv", rows), today=TODAY)
    run3, _ = match_book(world["a"], world["index"], loose, b2.batch_id)
    assert _matches(world["a"], run3)["X-2"]["confidence"] in (REVIEW, HIGH)
    if _matches(world["a"], run3)["X-2"]["confidence"] == HIGH:  # only the Phoenix one matches the city
        assert _matches(world["a"], run3)["X-2"]["company_id"] == world["ids"]["copper_phx"]


def test_shared_phone_between_two_companies_is_review(world, cfg, tmp_path):
    c = sqlite3.connect(world["intel"])
    c.execute("INSERT INTO company_contact_channels (company_id, contact_type, contact_value, normalized_value, "
              "source_family, discovered_at, verification_status, model_version, created_at, updated_at) "
              "VALUES (?, 'business_phone', '602-555-0101', '6025550101', 'synthetic', ?, 'VERIFIED', 'test', ?, ?)",
              (world["ids"]["paloverde"], NOW, NOW, NOW))
    c.commit()
    c.close()
    index = load_index(world["intel"], cfg)
    rows = [["S-1", "Saguaro Pipe Works", "", "", "Phoenix", "AZ", "", "602-555-0101", "", "", "", _days_ago(5)]]
    batch = import_book(world["a"], write_csv(tmp_path / "s.csv", rows), today=TODAY)
    run_id, _ = match_book(world["a"], index, cfg, batch.batch_id)
    m = _matches(world["a"], run_id)["S-1"]
    # Name + phone point at Saguaro; the phone also belongs to Palo Verde → review.
    assert m["confidence"] == REVIEW and "shares" in m["review_reason"]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def test_classification_with_default_policy(matched, cfg):
    classify_run(matched["a"], matched["run_id"], matched["index"], cfg)
    c = _classes(matched["a"], matched["run_id"])
    assert c["A-1001"]["classification"] == "ACTIVE_CUSTOMER_ACTIVE_MARKET"
    assert c["A-1001"]["market_trend"] == "RISING" and c["A-1001"]["wet_relevant"] == 1
    assert c["A-1002"]["classification"] == "DORMANT_CUSTOMER_QUIET_MARKET"
    assert c["A-1002"]["market_trend"] == "DECLINING"
    # No supplier "former" policy → old purchase is DORMANT, never inferred FORMER.
    assert c["A-1003"]["classification"] == "DORMANT_CUSTOMER_ACTIVE_MARKET"
    assert c["A-1006"]["classification"] == "DORMANT_CUSTOMER_ACTIVE_MARKET"
    assert c["A-1011"]["classification"] == "UNKNOWN_PURCHASE_STATUS"
    assert c["A-1005"]["classification"] == "IDENTITY_REVIEW"
    assert c["A-1004"]["classification"] == "UNMATCHED_ACCOUNT"
    assert all(x["classification"] != "FORMER_CUSTOMER_ACTIVE_MARKET" for x in c.values())


def test_former_requires_supplier_policy(matched):
    policy = BookMatchConfig(as_of=AS_OF, former_after_days=540).validate()
    classify_run(matched["a"], matched["run_id"], matched["index"], policy)
    c = _classes(matched["a"], matched["run_id"])
    assert c["A-1003"]["classification"] == "FORMER_CUSTOMER_ACTIVE_MARKET"   # 628 days
    assert c["A-1006"]["classification"] == "DORMANT_CUSTOMER_ACTIVE_MARKET"  # 400 days


def test_thresholds_are_configurable(matched):
    wide = BookMatchConfig(as_of=AS_OF, active_purchase_days=210).validate()
    classify_run(matched["a"], matched["run_id"], matched["index"], wide)
    assert _classes(matched["a"], matched["run_id"])["A-1002"]["classification"] == "ACTIVE_CUSTOMER_QUIET_MARKET"


@pytest.mark.parametrize("bad", [
    dict(active_purchase_days=0), dict(active_purchase_days=90, former_after_days=60),
    dict(market_window_days=0), dict(name_review_min_similarity=0.2), dict(as_of="not-a-date"),
])
def test_invalid_config_rejected(bad):
    with pytest.raises(ValueError):
        BookMatchConfig(**bad).validate()


def test_net_new_relevant_contractors(matched, cfg):
    counts = classify_run(matched["a"], matched["run_id"], matched["index"], cfg)
    with matched["a"].connect() as conn:
        nn = {r["company_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM net_new_candidates WHERE run_id=?", (matched["run_id"],))}
    ids = matched["ids"]
    assert ids["ocotillo"] in nn                 # wet-side, active, not confidently in the book
    assert nn[ids["ocotillo"]]["possible_account"] == "A-1010"  # flagged: an account under review may be it
    assert ids["saguaro"] not in nn and ids["agave"] not in nn   # already confident customers
    assert ids["paloverde"] not in nn            # GC lane is not wet-side
    assert ids["dsun_pool"] not in nn            # pools are not wet-side here
    assert ids["mesquite"] not in nn             # matched (and no recent permits)
    assert counts["NET_NEW_RELEVANT_CONTRACTOR"] == len(nn)


# ---------------------------------------------------------------------------
# Human review
# ---------------------------------------------------------------------------

def test_review_decisions_round_trip(matched, cfg, tmp_path):
    classify_run(matched["a"], matched["run_id"], matched["index"], cfg)
    folder = generate_reports(matched["a"], matched["run_id"], tmp_path / "out", matched["index"])
    review = list(csv.DictReader(open(folder / "review.csv", encoding="utf-8")))
    assert {r["supplier_account_id"] for r in review} == {"A-1005", "A-1007", "A-1008", "A-1010"}
    by_id = {r["supplier_account_id"]: r for r in review}
    assert by_id["A-1008"]["conflicting_evidence"] and by_id["A-1005"]["other_candidates"]
    by_id["A-1005"].update(decision="SET_COMPANY", decided_company_id=str(matched["ids"]["copper_phx"]),
                           reviewer="Synthetic Reviewer")
    by_id["A-1007"].update(decision="confirm")
    by_id["A-1010"].update(decision="REJECT", note="phone belongs to a different business")
    by_id["A-1008"].update(decision="MAYBE")
    done = tmp_path / "review_done.csv"
    with open(done, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(review[0].keys()))
        w.writeheader()
        w.writerows(by_id.values())
        w.writerow({**review[0], "supplier_account_id": "NOT-IN-BOOK", "decision": "CONFIRM"})
    res = apply_review_csv(matched["a"], done, matched["index"])
    assert res.applied == 3 and len(res.issues) == 2
    classify_run(matched["a"], matched["run_id"], matched["index"], cfg)
    c = _classes(matched["a"], matched["run_id"])
    assert c["A-1005"]["company_id"] == matched["ids"]["copper_phx"]
    assert c["A-1005"]["match_basis"] == "HUMAN_CONFIRMED"
    assert c["A-1005"]["classification"] == "ACTIVE_CUSTOMER_ACTIVE_MARKET"
    assert c["A-1007"]["match_basis"] == "HUMAN_CONFIRMED"
    assert c["A-1010"]["classification"] == "UNMATCHED_ACCOUNT" and c["A-1010"]["match_basis"] == "HUMAN_REJECTED"
    assert c["A-1008"]["classification"] == "IDENTITY_REVIEW"


# ---------------------------------------------------------------------------
# Reports, CSV safety, CLI
# ---------------------------------------------------------------------------

def test_reports_written_and_formula_safe(world, cfg, tmp_path):
    s = run_book_match(world["a"], write_csv(tmp_path / "book_a.csv", BOOK_A), tmp_path / "out", cfg,
                       index=world["index"])
    names = {p.name for p in s.report_dir.iterdir()}
    assert names == {"summary.md", "matches.csv", "review.csv", "classification.csv", "net_new.csv",
                     "import_issues.csv"}
    assert s.report_dir.name.startswith("supplier-a-")
    for p in s.report_dir.glob("*.csv"):
        for row in csv.reader(open(p, encoding="utf-8")):
            for cell in row:
                assert not cell.startswith(("=", "+", "-", "@")), (p.name, cell)
    matches = (s.report_dir / "matches.csv").read_text(encoding="utf-8")
    assert "'=HYPERLINK" in matches
    summary = (s.report_dir / "summary.md").read_text(encoding="utf-8")
    assert "Synthetic Supplier A" in summary and "Geography/branch proximity is not applied" in summary
    assert "not inferred (no supplier policy set)" in summary
    with pytest.raises(FileExistsError):  # reports are never overwritten
        (s.report_dir).mkdir()


def test_escape_round_trip():
    for v in ("=1+1", "+1 602", "-5", "@SUM(A1)", "\tx", "plain", "", "O'Neil"):
        assert unescape_cell(escape_cell(v)) == v
    assert escape_cell(None) == "" and escape_cell(5) == 5


def test_cli_end_to_end(world, tmp_path, capsys):
    from pipeline.book_match.__main__ import main
    root = tmp_path / "cli-root"
    assert main(["tenant-create", "--tenant", "cli-supplier", "--name", "CLI Supplier", "--tenant-root", str(root)]) == 0
    src = write_csv(tmp_path / "book.csv", BOOK_A)
    cfg_file = tmp_path / "cfg.json"
    cfg_file.write_text(json.dumps({"as_of": AS_OF, "former_after_days": 540}), encoding="utf-8")
    rc = main([str(src), "--tenant", "cli-supplier", "--out", str(tmp_path / "out"),
               "--tenant-root", str(root), "--intel-db", str(world["intel"]), "--config", str(cfg_file)])
    out = capsys.readouterr().out
    assert rc == 0 and "accepted 14" in out and "report:" in out
    assert main([str(src), "--tenant", "unknown-supplier", "--out", str(tmp_path / "o2"),
                 "--tenant-root", str(root), "--intel-db", str(world["intel"])]) == 3
    assert main([str(src), "--tenant", "../escape", "--out", str(tmp_path / "o3"),
                 "--tenant-root", str(root), "--intel-db", str(world["intel"])]) == 3


# ---------------------------------------------------------------------------
# Read-only intelligence + privacy
# ---------------------------------------------------------------------------

def test_intelligence_db_is_never_written(world, cfg, tmp_path):
    before = sha(world["intel"])
    run_book_match(world["a"], write_csv(tmp_path / "book_a.csv", BOOK_A), tmp_path / "out", cfg,
                   intel_db=world["intel"])
    assert sha(world["intel"]) == before
    conn = open_readonly(world["intel"])
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO companies (normalized_name, created_at, updated_at) VALUES ('X', 'x', 'x')")
    conn.close()


def test_logs_carry_no_account_values(world, cfg, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="corridoriq.book_match")
    run_book_match(world["a"], write_csv(tmp_path / "book_a.csv", BOOK_A), tmp_path / "out", cfg,
                   index=world["index"])
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "tenant=supplier-a" in text
    for secret in ("SAGUARO", "Saguaro", "555-0101", "6025550101", "Camelback", "Rep One", _days_ago(29),
                   "A-1001", "Mesquite"):
        assert secret not in text, secret


# ---------------------------------------------------------------------------
# Performance (synthetic)
# ---------------------------------------------------------------------------

def test_5000_account_book_performance(tmp_path, cfg):
    intel = tmp_path / "perf_intel.db"
    build_intel_db(intel, extra_companies=8000)
    reg = TenantRegistry(tmp_path / "root")
    reg.create("perf-supplier", "Perf Supplier")
    store = reg.store(reg.context("perf-supplier"))
    rows = []
    for i in range(5000):
        n = i % 8000
        rows.append([f"P-{i:05d}", f"Synthetic Trade Company {n:05d}" + (" L.L.C." if i % 3 else ""), "",
                     f"{100 + i} W Synthetic Rd", "Phoenix", "AZ", "85001",
                     f"602-555-{i % 10000:04d}" if i % 2 else "", "", "Central", "Rep", _days_ago(i % 700)])
    src = write_csv(tmp_path / "perf.csv", rows)
    t0 = time.perf_counter()
    s = run_book_match(store, src, tmp_path / "out", cfg, intel_db=intel)
    total = time.perf_counter() - t0
    assert s.batch.accepted == 5000
    assert sum(s.match_counts.values()) == 5000
    assert total < 120, s.timings


def test_sql_injection_payloads_are_stored_literally(world, cfg, tmp_path):
    payload = "x'); DROP TABLE book_accounts; --"
    rows = [["INJ-1", payload, "Robert'); DELETE FROM review_decisions;--", payload, payload, "AZ", "85001",
             "", "", payload, payload, ""],
            [payload[:60], "Normal Co", "", "", "", "", "", "", "", "", "", ""]]
    s = run_book_match(world["a"], write_csv(tmp_path / "inj.csv", rows), tmp_path / "out", cfg,
                       index=world["index"])
    assert s.batch.accepted == 2
    with world["a"].connect() as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        stored = conn.execute("SELECT company_name FROM book_accounts WHERE supplier_account_id='INJ-1'").fetchone()[0]
        odd_id = conn.execute("SELECT COUNT(*) FROM book_accounts WHERE supplier_account_id=?",
                              (payload[:60],)).fetchone()[0]
    assert {"book_accounts", "review_decisions", "account_matches"} <= tables
    assert stored == payload and odd_id == 1
