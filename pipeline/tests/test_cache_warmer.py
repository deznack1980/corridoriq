"""Background warmer: after a refresh, the first dashboard visit is served from cache."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from pipeline.api import warmer
from pipeline.crm import scan_cache
from pipeline.crm import service as crm
from pipeline.trust import account_view as trust
from pipeline.tests.test_trust_portal import world  # noqa: F401  (fixture)
from pipeline.tests.test_workspace_ui import conn, env  # noqa: F401  (fixtures)


@pytest.fixture(autouse=True)
def _fresh():
    scan_cache.clear()
    trust.clear_cache()
    yield
    scan_cache.clear()
    trust.clear_cache()


@pytest.fixture()
def db_path(world, tmp_path):
    path = tmp_path / "portal.db"
    disk = sqlite3.connect(path)
    world["conn"].backup(disk)
    disk.close()
    return path


def _request_conn(path):
    """How the portal opens the database for a request: plain path, read-write."""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _admin(c):
    from pipeline.auth import service as auth

    return auth.build_user_context(c, c.execute("SELECT * FROM users WHERE email='admin@corridoriq.com'").fetchone())


def test_warm_once_fills_both_caches_for_request_connections(db_path, monkeypatch):
    sig, report = warmer.warm_once(db_path)
    assert report["warmed"] and report["queue_seconds"] is not None and report["scan_seconds"]
    # A request on the portal's own kind of connection now hits both caches:
    # no queue rebuild, and no shared-scan miss for the list or the map.
    calls = {"assemble": 0}
    import pipeline.trust.opportunities as opp

    real_assemble = opp.assemble
    monkeypatch.setattr(opp, "assemble", lambda *a, **k: (calls.__setitem__("assemble", calls["assemble"] + 1), real_assemble(*a, **k))[1])
    c = _request_conn(db_path)
    assert trust.queue_cached(c)
    crm.dashboard(c, _admin(c))  # Today's Accounts
    misses = scan_cache.stats["misses"]
    crm.opportunities(c, _admin(c), {"context": "organization", "page_size": 25})
    crm.opportunity_map(c, _admin(c), {"context": "organization"})
    c.close()
    assert calls["assemble"] == 0
    assert scan_cache.stats["misses"] == misses


def test_warm_results_equal_request_results(db_path):
    c = _request_conn(db_path)
    cold = crm.opportunity_map(c, _admin(c), {"context": "organization"})
    cold_queue = crm.dashboard(c, _admin(c))["todays_accounts"]
    c.close()
    scan_cache.clear()
    trust.clear_cache()
    warmer.warm_once(db_path)
    c = _request_conn(db_path)
    assert crm.opportunity_map(c, _admin(c), {"context": "organization"}) == cold
    assert crm.dashboard(c, _admin(c))["todays_accounts"] == cold_queue
    c.close()


def test_warms_again_only_after_a_refresh(db_path):
    sig, first = warmer.warm_once(db_path)
    sig2, second = warmer.warm_once(db_path, sig)
    assert first["warmed"] and not second["warmed"] and sig2 == sig
    c = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c.execute("INSERT INTO pipeline_runs (run_type, status, started_at, completed_at, trigger_source, created_at) "
              "VALUES ('morning_refresh', 'succeeded', ?, ?, 'test', ?)", (now, now, now))
    c.commit()
    c.close()
    sig3, third = warmer.warm_once(db_path, sig2)
    assert third["warmed"] and sig3 != sig2


def test_warmer_never_writes(db_path):
    before = db_path.read_bytes()
    warmer.warm_once(db_path)
    assert db_path.read_bytes() == before


def test_warmer_can_be_disabled(monkeypatch):
    monkeypatch.setenv("CORRIDORIQ_CACHE_WARMER", "0")
    assert not warmer.enabled()
    monkeypatch.delenv("CORRIDORIQ_CACHE_WARMER")
    assert warmer.enabled()
