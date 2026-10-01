"""Tenant isolation for supplier-owned customer books.

Tests the storage / data-access boundary directly (there is no UI here):
Supplier A's store can never read, write, report on, or classify Supplier B's
data; missing, forged, invalid, inactive, or path-traversing tenant identities
fail closed. Synthetic data only; everything lives in temporary directories.
"""

from __future__ import annotations

import csv
import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from pipeline.book_match.classify import classify_run
from pipeline.book_match.config import BookMatchConfig
from pipeline.book_match.importer import import_book
from pipeline.book_match.matcher import match_book
from pipeline.book_match.report import generate_reports
from pipeline.book_match.run import load_index, run_book_match
from pipeline.tenancy import TenantContext, TenantError, TenantRegistry, TenantStore, require_context
from pipeline.tenancy.context import issue_context
from pipeline.tenancy.model import Tenant, TenantType, validate_tenant_id
from pipeline.tests.test_book_match import AS_OF, BOOK_A, TODAY, build_intel_db, write_csv

B_MARKER = "Zephyr Secret Plumbing Beta"
BOOK_B = [
    ["B-9001", B_MARKER, "", "1 Hidden Way", "Phoenix", "AZ", "85001", "602-555-0909", "", "Beta Branch",
     "Beta Rep", "2026-09-01"],
    ["B-9002", "Saguaro Pipe Works", "", "", "Phoenix", "AZ", "", "(602) 555-0101", "", "Beta Branch",
     "Beta Rep", "2026-01-01"],
]


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture()
def two(tmp_path):
    intel = tmp_path / "intel.db"
    build_intel_db(intel)
    cfg = BookMatchConfig(as_of=AS_OF).validate()
    reg = TenantRegistry(tmp_path / "root")
    reg.create("supplier-a", "Supplier A")
    reg.create("supplier-b", "Supplier B")
    a, b = reg.store(reg.context("supplier-a")), reg.store(reg.context("supplier-b"))
    index = load_index(intel, cfg)
    sa = run_book_match(a, write_csv(tmp_path / "a.csv", BOOK_A), tmp_path / "out", cfg, index=index)
    sb = run_book_match(b, write_csv(tmp_path / "b.csv", BOOK_B), tmp_path / "out", cfg, index=index)
    return {"tmp": tmp_path, "reg": reg, "a": a, "b": b, "sa": sa, "sb": sb, "cfg": cfg, "index": index,
            "intel": intel}


def _all_values(store, table) -> str:
    with store.connect() as conn:
        return repr([tuple(r) for r in conn.execute(f"SELECT * FROM {table}")])


# ---- tenant identity --------------------------------------------------------

@pytest.mark.parametrize("bad", [
    None, "", 123, "ab", "a" * 49, "../supplier-b", "..", ".", "a/b", "a\\b", "C:\\x", "/abs", "supplier_b",
    "Supplier-A", "supplier a", "-abc", "abc-", "ab--cd", "café-supply", "con", "nul", "com1", "lpt9",
    "tenants", "registry", "default", "%2e%2e", "supplier-a\x00", "supplier-a/../supplier-b",
])
def test_invalid_tenant_ids_fail_closed(tmp_path, bad):
    reg = TenantRegistry(tmp_path / "root")
    with pytest.raises(TenantError):
        validate_tenant_id(bad)
    with pytest.raises(TenantError):
        reg.create(bad, "X")
    with pytest.raises(TenantError):
        reg.context(bad)
    with pytest.raises(TenantError):
        reg.tenant_dir(bad)


def test_valid_ids_resolve_inside_the_tenant_root(tmp_path):
    reg = TenantRegistry(tmp_path / "root")
    for tid in ("abc", "supplier-a", "a1-b2-c3", "x" * 48):
        assert validate_tenant_id(tid) == tid
        d = reg.tenant_dir(tid)
        assert d.parent == (tmp_path / "root" / "tenants").resolve() and d.name == tid


def test_registry_requires_explicit_root():
    for root in (None, "", "   "):
        with pytest.raises(TenantError):
            TenantRegistry(root)


def test_duplicate_and_unknown_tenants(tmp_path):
    reg = TenantRegistry(tmp_path / "root")
    reg.create("supplier-a", "A")
    with pytest.raises(TenantError):
        reg.create("supplier-a", "A again")
    with pytest.raises(TenantError):
        reg.context("supplier-z")


def test_only_supplier_tenants_supported(tmp_path):
    reg = TenantRegistry(tmp_path / "root")
    with pytest.raises(ValueError):
        reg.create("contractor-a", "C", tenant_type="contractor")
    assert [t.value for t in TenantType] == ["supplier"]


# ---- context: no default, no forgery ----------------------------------------

def test_missing_context_fails_closed(two):
    for bad in (None, "supplier-a", {"tenant_id": "supplier-a"}, two["a"].tenant):
        with pytest.raises(TenantError):
            require_context(bad)
        with pytest.raises(TenantError):
            TenantStore(bad, two["reg"])
        with pytest.raises(TenantError):
            two["reg"].store(bad)


def test_context_cannot_be_constructed_directly(two):
    tenant = two["reg"].get("supplier-b")
    with pytest.raises(TenantError):
        TenantContext(tenant)
    with pytest.raises(TenantError):
        TenantContext(tenant, object())


def test_inactive_tenant_fails_closed_even_for_bound_stores(two):
    two["reg"].set_active("supplier-b", False)
    with pytest.raises(TenantError):
        two["reg"].context("supplier-b")
    with pytest.raises(TenantError):
        with two["b"].connect():
            pass
    with pytest.raises(TenantError):
        issue_context(two["reg"].get("supplier-b"))  # inactive tenant cannot even be issued
    two["reg"].set_active("supplier-b", True)
    with two["b"].connect():
        pass


def test_forged_context_for_unregistered_tenant_is_refused(two):
    ghost = Tenant("supplier-z", "Ghost", TenantType.SUPPLIER, True, "2026-01-01")
    with pytest.raises(TenantError):
        two["reg"].store(issue_context(ghost))


# ---- read isolation ----------------------------------------------------------

@pytest.mark.parametrize("table", ["book_accounts", "account_normalized", "account_matches",
                                   "account_classifications", "import_batches", "import_issues",
                                   "net_new_candidates", "match_runs"])
def test_supplier_a_cannot_read_supplier_b(two, table):
    a_text = _all_values(two["a"], table)
    assert B_MARKER not in a_text and "B-9001" not in a_text and "B-9002" not in a_text
    assert "supplier-b" not in a_text
    b_text = _all_values(two["b"], table)
    assert "A-1001" not in b_text and "supplier-a" not in b_text


def test_each_store_reports_its_own_owner(two):
    with two["a"].connect() as conn:
        assert conn.execute("SELECT tenant_id FROM tenant_meta").fetchone()[0] == "supplier-a"
        assert {r[0] for r in conn.execute("SELECT DISTINCT tenant_id FROM book_accounts")} == {"supplier-a"}
    assert two["a"].directory != two["b"].directory


def test_reports_contain_only_their_own_tenant(two):
    a_dir, b_dir = two["sa"].report_dir, two["sb"].report_dir
    assert a_dir != b_dir and a_dir.name.startswith("supplier-a-") and b_dir.name.startswith("supplier-b-")
    a_blob = "".join(p.read_text(encoding="utf-8") for p in a_dir.iterdir())
    b_blob = "".join(p.read_text(encoding="utf-8") for p in b_dir.iterdir())
    assert B_MARKER not in a_blob and "B-9001" not in a_blob and "Beta Rep" not in a_blob
    assert "A-1001" not in b_blob and "Rep One" not in b_blob
    assert "Supplier B" not in a_blob and "Supplier A" not in b_blob


def test_cross_tenant_run_ids_are_not_found(two, tmp_path):
    with pytest.raises(LookupError):
        generate_reports(two["a"], two["sb"].run_id, tmp_path / "x")
    with pytest.raises(LookupError):
        classify_run(two["a"], two["sb"].run_id, two["index"], two["cfg"])
    with two["a"].connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM match_runs WHERE run_id=?", (two["sb"].run_id,)).fetchone()[0] == 0


def test_report_generation_cannot_aggregate_tenants():
    import inspect
    params = list(inspect.signature(generate_reports).parameters)
    assert params[:2] == ["store", "run_id"]  # exactly one store; no tenant list, no path to a database
    assert "stores" not in params and "tenants" not in params


# ---- write isolation -----------------------------------------------------------

def test_supplier_a_activity_never_changes_supplier_b(two, tmp_path):
    b_db = two["b"].directory / "tenant.db"
    before = _sha(b_db)
    a = two["a"]
    batch = import_book(a, write_csv(tmp_path / "a2.csv", BOOK_A[:5]), today=TODAY)
    run_id, _ = match_book(a, two["index"], two["cfg"], batch.batch_id)
    classify_run(a, run_id, two["index"], two["cfg"])
    generate_reports(a, run_id, tmp_path / "out2", two["index"])
    assert _sha(b_db) == before


def test_swapped_store_file_fails_closed(two):
    """Copying B's database into A's directory must not expose B's book to A."""
    a_db = two["a"].directory / "tenant.db"
    shutil.copyfile(two["b"].directory / "tenant.db", a_db)
    with pytest.raises(TenantError):
        with two["a"].connect():
            pass


def test_symlinked_tenant_directory_is_refused(tmp_path):
    reg = TenantRegistry(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "root" / "tenants").mkdir(parents=True)
    link = tmp_path / "root" / "tenants" / "supplier-x"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        try:  # Windows: a directory junction needs no special privilege
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))
        except Exception:
            pytest.skip("neither symlinks nor junctions can be created here")
    with pytest.raises(TenantError):
        reg.tenant_dir("supplier-x")


def test_store_exposes_no_cross_tenant_api(two):
    public = {n for n in dir(two["a"]) if not n.startswith("_")}
    assert public == {"tenant_id", "tenant", "directory", "connect", "transaction", "ensure_schema"}


# ---- shared index / caches ---------------------------------------------------

def test_shared_index_holds_no_tenant_data(two):
    """The only cache-like object (the read-only intelligence index) is built
    from shared data and is reused across tenants without absorbing either book."""
    idx = two["index"]
    blob = repr(vars(idx)) + repr([vars(f) for f in idx.companies.values()])
    for secret in (B_MARKER, "B-9001", "A-1001", "Beta Rep", "Rep One", "602-555-0909", "6025550909"):
        assert secret not in blob, secret


def test_registry_holds_no_customer_data(two):
    reg_db = two["reg"].root / "registry.db"
    conn = sqlite3.connect(reg_db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    blob = repr(conn.execute("SELECT * FROM tenants").fetchall())
    conn.close()
    assert tables == {"tenants"}
    assert "A-1001" not in blob and B_MARKER not in blob
