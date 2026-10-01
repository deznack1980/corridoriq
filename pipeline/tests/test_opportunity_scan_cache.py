"""The shared opportunity scan: one scan per data signature, identical results."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

import pytest

from pipeline.auth import service as auth
from pipeline.crm import scan_cache
from pipeline.crm import service as crm
from pipeline.trust import account_view as trust
from pipeline.tests.test_trust_contacts import _chan
from pipeline.tests.test_trust_portal import _permit_project, world  # noqa: F401  (fixture)
from pipeline.tests.test_workspace_ui import _company, conn, env  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _fresh_cache():
    scan_cache.clear()
    trust.clear_cache()
    yield
    scan_cache.clear()


def _file_copy(mem: sqlite3.Connection, path) -> sqlite3.Connection:
    disk = sqlite3.connect(path, check_same_thread=False)
    mem.backup(disk)
    disk.row_factory = sqlite3.Row
    return disk


def _ctx(c, email):
    return auth.build_user_context(c, c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone())


def _strip(res):
    """Everything the caller sees, in order."""
    return {k: v for k, v in res.items()}


CASES = [
    {"context": "organization"},
    {"context": "organization", "page_size": 1, "page": 2},
    {"context": "organization", "scope": "all"},
    {"context": "organization", "scope": "all", "page_size": 1, "page": 2},
    {"context": "organization", "q": "Main"},
    {"context": "organization", "score_min": 80},
    {"context": "assigned"},
    {"context": "assigned", "scope": "all"},
]


@pytest.mark.parametrize("filters", CASES, ids=lambda f: "-".join(f"{k}={v}" for k, v in f.items()))
@pytest.mark.parametrize("all_rows", [False, True])
def test_cached_results_equal_uncached(world, tmp_path, filters, all_rows):
    mem = world["conn"]
    disk = _file_copy(mem, tmp_path / "w.db")
    for email in ("admin@corridoriq.com", "mgr@corridoriq.com", "rep@corridoriq.com"):
        if filters["context"] == "organization" and email.startswith("rep"):
            continue
        expected = crm.opportunities(mem, _ctx(mem, email), dict(filters), all_rows=all_rows)  # never cached
        first = crm.opportunities(disk, _ctx(disk, email), dict(filters), all_rows=all_rows)
        second = crm.opportunities(disk, _ctx(disk, email), dict(filters), all_rows=all_rows)  # from cache
        assert _strip(first) == _strip(expected), email
        assert _strip(second) == _strip(expected), email
    m_mem = crm.opportunity_map(mem, world["admin"], {"context": "organization"})
    m_disk = crm.opportunity_map(disk, _ctx(disk, "admin@corridoriq.com"), {"context": "organization"})
    assert m_disk == m_mem
    disk.close()


def test_list_and_map_share_one_scan(world, tmp_path, monkeypatch):
    path = tmp_path / "w.db"
    _file_copy(world["conn"], path).close()
    calls = {"n": 0}
    real = crm.trust.annotate_projects

    def counting(*a, **k):
        calls["n"] += 1
        return real(*a, **k)

    monkeypatch.setattr(crm.trust, "annotate_projects", counting)
    barrier = threading.Barrier(2)
    results, errors = {}, []

    def run(name, fn):
        c = sqlite3.connect(path)
        c.row_factory = sqlite3.Row
        try:
            user = _ctx(c, "admin@corridoriq.com")
            barrier.wait()
            results[name] = fn(c, user)
        except Exception as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            c.close()

    threads = [
        threading.Thread(target=run, args=("list", lambda c, u: crm.opportunities(c, u, {"context": "organization", "page_size": 25}))),
        threading.Thread(target=run, args=("map", lambda c, u: crm.opportunity_map(c, u, {"context": "organization"}))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert calls["n"] == 1  # one classification pass served both requests
    assert results["map"]["total"] == results["list"]["total"]
    assert scan_cache.stats["misses"] == 2  # one count + one scan


def test_new_data_invalidates(world, tmp_path):
    disk = _file_copy(world["conn"], tmp_path / "w.db")
    admin = _ctx(disk, "admin@corridoriq.com")
    before = crm.opportunities(disk, admin, {"context": "organization"})["total"]
    _permit_project(disk, world["co1"], "Replace water service line", score=77, days_ago=2)
    after = crm.opportunities(disk, admin, {"context": "organization"})
    assert after["total"] == before + 1
    assert any(i["trade_scope"].startswith("plumbing") and i["opportunity_score"] == 77 for i in after["items"])
    disk.close()


def test_crm_assignment_invalidates_assigned_context(world, tmp_path):
    disk = _file_copy(world["conn"], tmp_path / "w.db")
    mgr = _ctx(disk, "mgr@corridoriq.com")
    before = {i["company_id"] for i in crm.opportunities(disk, mgr, {"context": "assigned", "scope": "all"}, all_rows=True)["items"]}
    newco = _company(disk, "Gamma Plumbing")
    _permit_project(disk, newco, "Replace water heater", score=66, days_ago=1)
    crm.assign_company(disk, _ctx(disk, "admin@corridoriq.com"), newco, world["ids"]["rep"])
    after = {i["company_id"] for i in crm.opportunities(disk, mgr, {"context": "assigned", "scope": "all"}, all_rows=True)["items"]}
    assert newco not in before and newco in after
    disk.close()


def test_lane_rebuild_invalidates(world, tmp_path):
    disk = _file_copy(world["conn"], tmp_path / "w.db")
    admin = _ctx(disk, "admin@corridoriq.com")
    first = {i["company_id"] for i in crm.opportunities(disk, admin, {"context": "organization"}, all_rows=True)["items"]}
    assert world["co1"] in first
    # co1 moves to an off-focus lane (as a rebuild would do): its projects leave the view.
    disk.execute("UPDATE company_sales_lanes SET lane_key='SPECIALTY_OTHER', created_at=? WHERE company_id=?",
                 (datetime.now(timezone.utc).isoformat(), world["co1"]))
    disk.commit()
    second = {i["company_id"] for i in crm.opportunities(disk, admin, {"context": "organization"}, all_rows=True)["items"]}
    assert world["co1"] not in second
    disk.close()


def test_cache_is_not_mutated_and_contacts_stay_live(world, tmp_path):
    disk = _file_copy(world["conn"], tmp_path / "w.db")
    admin = _ctx(disk, "admin@corridoriq.com")
    one = crm.opportunities(disk, admin, {"context": "organization"}, all_rows=True)
    target = next(i for i in one["items"] if i["company_id"] == world["co1"])
    assert target["contact"]["status"] == "NOT_VERIFIED"
    target["display_name"] = "TAMPERED"
    target["location"] = {"lat": 0}
    _chan(disk, world["co1"], "business_phone", "602-555-0140", name="Pat Buyer")
    two = crm.opportunities(disk, admin, {"context": "organization"}, all_rows=True)
    again = next(i for i in two["items"] if i["company_id"] == world["co1"])
    assert again["display_name"] != "TAMPERED" and again["location"] is None
    assert again["contact"]["phone"] == "602-555-0140"  # contacts are read per request
    disk.close()


def test_in_memory_databases_are_never_cached(world):
    crm.opportunities(world["conn"], world["admin"], {"context": "organization"})
    crm.opportunities(world["conn"], world["admin"], {"context": "organization"})
    assert scan_cache.stats == {"hits": 0, "misses": 0, "waits": 0}
