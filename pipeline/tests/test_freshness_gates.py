"""Stale data must never look current.

Covers the outbound-readiness fixes: one recent window on source activity
dates for every opportunity feed, per-row age and feed-freshness labels,
age-aware refresh status, admin KPIs and freshness that fail closed, CEO brief
and health currency, and generated-report labels. Historical data is never
deleted; it is labelled or kept out of "current" views.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agents.ceo.analytics.brief import _top_accounts
from agents.ceo.analytics.serve import load_owner_brief_view, resolve_health
from pipeline import pipeline_runs
from pipeline.crm import admin as crm_admin
from pipeline.crm import service as crm
from pipeline.reports import catalog as reports_catalog
from pipeline.tests.test_ceo_morning_brief import _brief, _conn as _brief_conn, _write
from pipeline.tests.test_trust_portal import world  # noqa: F401  (fixture)
from pipeline.tests.test_workspace_ui import conn, env  # noqa: F401  (fixtures)
from pipeline.trust import contract as C
from pipeline.trust import gates as G
from pipeline.trust import recency
from pipeline.trust.health import collect_health

UTC = timezone.utc


def _day(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _ts(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat(timespec="seconds")


def _permit(c, company_id, number, description="Replace 50 gal water heater", *, issued=None, filed=None,
            opp=None, jurisdiction="phoenix_az", score=95, lifecycle="Permit Issued"):
    """A wet-scope permit + project with explicit source dates. The ingestion
    timestamps are always 'now', so any feed that used them would call it new."""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    cur = c.execute(
        "INSERT INTO permits (jurisdiction, permit_number, permit_type, description, job_address, city, state, "
        "status, contractor_company_id, issued_date, filed_date, first_seen_at, last_updated_at) "
        "VALUES (?,?,'Building',?,'9 Old Rd','PHOENIX','AZ','issued',?,?,?,?,?)",
        (jurisdiction, number, description, company_id, issued, filed, now, now))
    c.execute(
        "INSERT INTO projects (permit_id, jurisdiction, contractor_company_id, project_category, "
        "project_lifecycle, opportunity_score, opportunity_date, opportunity_timing) "
        "VALUES (?,?,?,'commercial',?,?,?,'immediate')",
        (cur.lastrowid, jurisdiction, company_id, lifecycle, score, opp))
    c.commit()
    return cur.lastrowid


def _project_ids(res):
    return {i["project_id"] for i in res["items"]}


def _pid(c, number):
    return c.execute("SELECT pr.id FROM projects pr JOIN permits p ON p.id=pr.permit_id "
                     "WHERE p.permit_number=?", (number,)).fetchone()[0]


def _run(c, status, completed_hours_ago, *, summary=None, started_hours_ago=None):
    started = _ts((started_hours_ago if started_hours_ago is not None else (completed_hours_ago or 0) + 0.2))
    completed = None if completed_hours_ago is None else _ts(completed_hours_ago)
    c.execute(
        "INSERT INTO pipeline_runs (run_type, trigger_source, started_at, completed_at, status, summary_json, created_at) "
        "VALUES ('morning_refresh','cli',?,?,?,?,?)",
        (started, completed, status, json.dumps(summary) if summary else None, started))
    c.commit()


# ------------------------------------------------------------ opportunity feeds


def test_old_high_score_permit_is_not_a_current_opportunity(world):
    """A 400-day-old permit with the best score must not top any current feed."""
    c = world["conn"]
    _permit(c, world["co1"], "OLD-400", issued=_day(400), opp=_day(400), score=100)
    old = _pid(c, "OLD-400")
    for user in (world["rep"], world["mgr"], world["admin"]):
        res = crm.opportunities(c, user, {"context": "assigned"})
        assert old not in _project_ids(res)
        assert res["recency_window"]["days"] == recency.REVIEW_WINDOW_DAYS
        assert res["items"], "recent projects still listed"
        assert all(i["recency"] in (recency.RECENT, recency.OUTSIDE_CALL_WINDOW) for i in res["items"])
    org = crm.opportunities(c, world["admin"], {"context": "organization", "scope": "all"})
    assert old not in _project_ids(org)
    # Dashboard Top Opportunities is page 1 of the same call; the map is the same feed.
    top = crm.opportunities(c, world["rep"], {"context": "assigned", "page_size": 6})
    assert old not in _project_ids(top)
    for ctx in ("assigned", "organization"):
        m = crm.opportunity_map(c, world["admin"], {"context": ctx, "scope": "all"})
        assert old not in {p["project_id"] for p in m["points"]}
    # The history is still in the company record (nothing deleted).
    history = crm.get_company_projects(c, world["mgr"], world["co1"])
    assert any(int(p.get("project_id") or p.get("id")) == old for p in history)


def test_history_is_opt_in_assigned_only_and_labelled(world):
    c = world["conn"]
    _permit(c, world["co1"], "OLD-200", issued=_day(200), opp=_day(200), score=100)
    old = _pid(c, "OLD-200")
    res = crm.opportunities(c, world["rep"], {"context": "assigned", "recency": "all"})
    row = next(i for i in res["items"] if i["project_id"] == old)
    assert row["recency"] == recency.HISTORICAL
    assert row["days_since_activity"] == 200
    assert "history, not a current opportunity" in row["freshness_note"]
    assert res["recency_window"]["history_included"] is True
    # recency=all does not lift the organization window.
    org = crm.opportunities(c, world["admin"], {"context": "organization", "recency": "all", "scope": "all"})
    assert old not in _project_ids(org)
    assert org["recency_window"]["history_included"] is False


def test_undated_and_future_dated_permits_are_never_recent(world):
    c = world["conn"]
    _permit(c, world["co1"], "UNDATED", issued=None, filed=None, opp=None, score=100)
    _permit(c, world["co1"], "FUTURE", issued=_day(-30), opp=_day(-30), score=100)
    undated, future = _pid(c, "UNDATED"), _pid(c, "FUTURE")
    for ctx in ("assigned", "organization"):
        ids = _project_ids(crm.opportunities(c, world["admin"], {"context": ctx, "scope": "all"}))
        assert undated not in ids and future not in ids, ctx
    dash = crm.dashboard(c, world["rep"])
    assert {i["project_id"] for i in dash["recent_opportunity_activity"]}.isdisjoint({undated, future})
    admin = crm_admin.admin_dashboard(c, world["admin"])
    assert {i["project_id"] for i in admin["recent_opportunity_activity"]}.isdisjoint({undated, future})
    # Labelled, never dated, when history is requested.
    hist = crm.opportunities(c, world["rep"], {"context": "assigned", "recency": "all"})
    for pid in (undated, future):
        row = next(i for i in hist["items"] if i["project_id"] == pid)
        assert row["recency"] == recency.UNDATED and row["days_since_activity"] is None


def test_rows_carry_age_and_call_window_labels(world):
    c = world["conn"]
    _permit(c, world["co1"], "MID-45", issued=_day(45), opp=_day(45), score=100)
    mid = _pid(c, "MID-45")
    res = crm.opportunities(c, world["mgr"], {"context": "assigned"})
    row = next(i for i in res["items"] if i["project_id"] == mid)
    assert row["activity_date"] == _day(45) and row["activity_basis"] == "issued"
    assert row["days_since_activity"] == 45
    assert row["recency"] == recency.OUTSIDE_CALL_WINDOW
    assert "outside the 30-day call window" in row["freshness_note"]
    fresh = [i for i in res["items"] if i["recency"] == recency.RECENT]
    assert fresh and all(i["source_freshness"] == "FRESH" and not i["source_stale"] for i in fresh)
    # Raw source date columns are not added to the payload.
    assert "issued_date" not in row and "filed_date" not in row


def test_stale_jurisdiction_rows_are_labelled(world):
    """A city whose newest record is 40 days old is STALE (the trust layer's
    rule); its rows are shown labelled, never as current."""
    c = world["conn"]
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) VALUES ('mesa_az','Mesa','AZ','connected')")
    c.commit()
    _permit(c, world["co1"], "MESA-40", issued=_day(40), opp=_day(40), jurisdiction="mesa_az", score=100)
    mesa = _pid(c, "MESA-40")
    res = crm.opportunities(c, world["mgr"], {"context": "assigned"})
    row = next(i for i in res["items"] if i["project_id"] == mesa)
    assert row["source_freshness"] == "STALE" and row["source_stale"] is True
    assert "feed is not current" in row["freshness_note"]
    assert res["stale_source_rows"] >= 1
    # A jurisdiction no longer connected (status pending) has no current feed,
    # even for a recent-looking permit already in the database.
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) VALUES ('ghost_az','Ghost','AZ','pending')")
    _permit(c, world["co1"], "GHOST-5", issued=_day(5), opp=_day(5), jurisdiction="ghost_az", score=100)
    ghost = _pid(c, "GHOST-5")
    row = next(i for i in crm.opportunities(c, world["mgr"], {"context": "assigned"})["items"]
               if i["project_id"] == ghost)
    assert row["source_freshness"] == "NO_DATA" and row["source_stale"] is True


def test_dashboard_activity_lists_use_the_same_window(world):
    c = world["conn"]
    _permit(c, world["co1"], "OLD-90", issued=_day(90), opp=_day(90), score=100)
    old = _pid(c, "OLD-90")
    for feed in (crm.dashboard(c, world["rep"])["recent_opportunity_activity"],
                 crm.recent_opportunity_activity(c, world["mgr"]),
                 crm_admin.admin_dashboard(c, world["admin"])["recent_opportunity_activity"]):
        assert old not in {i["project_id"] for i in feed}
        assert feed and all(i["days_since_activity"] is not None and i["days_since_activity"] <= 60 for i in feed)
        dates = [i["activity_date"] for i in feed]
        assert dates == sorted(dates, reverse=True)


# ------------------------------------------------------------ refresh status


def test_refresh_label_needs_a_recent_success(conn):
    assert pipeline_runs.employee_status(conn)["stale"] is True  # no refresh yet
    _run(conn, "succeeded", 3 * 24)
    old = pipeline_runs.employee_status(conn)
    assert old["status"] == "succeeded"
    assert old["label"] == pipeline_runs.STALE_LABEL != "Data current"
    assert old["stale"] is True and old["freshness"] == "stale"
    _run(conn, "succeeded", 2)
    now = pipeline_runs.employee_status(conn)
    assert now["label"] == "Data current" and now["stale"] is False
    # The threshold is the trust layer's 36 hours, decided against an injectable clock.
    later = datetime.now(UTC) + timedelta(hours=35)
    assert pipeline_runs.employee_status(conn, now=later)["label"] == pipeline_runs.STALE_LABEL


def test_failed_partial_or_running_refresh_never_makes_old_data_current(conn):
    _run(conn, "succeeded", 4 * 24)
    _run(conn, "failed", 1)
    failed = pipeline_runs.employee_status(conn)
    assert failed["label"] == "Data refresh failed" and failed["stale"] is True
    _run(conn, "partial", 0.5)
    assert pipeline_runs.employee_status(conn)["stale"] is True
    _run(conn, "running", None, started_hours_ago=0.1)
    running = pipeline_runs.employee_status(conn)
    assert running["label"] == "Refresh in progress"
    assert running["last_completed"] is not None  # the previous finished run, not "—"
    assert running["stale"] is True


def test_queue_refresh_gate_fails_closed():
    base = dict(
        ctx={"company": {"lifecycle_state": "active"},
             "lanes": [{"lane_key": "PLUMBING_CORE", "fit": "HIGH", "presentable": 1}]},
        lane="PLUMBING_CORE", scope={"explicit": True}, activity_days=3,
        source={"freshness": "FRESH", "call_blocked": False, "jurisdiction": "phoenix_az"},
        role={"role": G.TRADE_CONTRACTOR, "basis": "permit", "confidence": 90.0, "level": "permit"},
        identity={"status": G.VERIFIED, "basis": "ROC", "flags": []},
        contact={"state": G.VERIFIED_PHONE},
    )
    assert G.evaluate(refresh_state="CURRENT", **base)["action"] == C.CALL_NOW
    aging = G.evaluate(refresh_state="AGING", **base)
    assert aging["confidence"] == C.MEDIUM and "REFRESH_AGING" in aging["flags"]
    for state in ("STALE", "UNKNOWN"):
        assert G.evaluate(refresh_state=state, **base)["action"] == C.HOLD
    old = dict(base, activity_days=G.CALL_WINDOW_DAYS + 1)
    assert G.evaluate(refresh_state="CURRENT", **old)["action"] not in C.QUEUE_ACTIONS


def test_newer_failed_run_is_not_a_successful_refresh(conn):
    _run(conn, "succeeded", 10 * 24)
    _run(conn, "failed", 1)
    health = collect_health(conn, datetime.now(UTC))
    assert health["refresh"]["state"] == "STALE"
    assert health["refresh"]["latest_run"]["status"] == "failed"


# ------------------------------------------------------------ admin dashboard


def test_admin_kpis_do_not_reuse_an_old_summary_as_today(env):
    c = env["conn"]
    summary = {"new_submitted_opportunities": 12, "new_issued_permits": 7, "completed_at": _ts(72)}
    _run(c, "succeeded", 72, summary=summary)
    d = crm_admin.admin_dashboard(c, env["admin"])
    assert d["kpis"]["new_submitted_opportunities"] == 0
    assert d["kpis"]["new_issued_permits"] == 0
    assert d["morning_refresh"]["summary_is_today"] is False
    assert d["morning_refresh"]["stale"] is True
    assert d["morning_refresh"]["label"] == pipeline_runs.STALE_LABEL
    # A run that completed today may still fill an empty live count.
    today = {"new_submitted_opportunities": 12, "new_issued_permits": 7,
             "completed_at": datetime.now(UTC).isoformat(timespec="seconds")}
    _run(c, "succeeded", 0, summary=today)
    d = crm_admin.admin_dashboard(c, env["admin"])
    assert d["morning_refresh"]["summary_is_today"] is True
    assert d["kpis"]["new_submitted_opportunities"] == 12
    assert d["morning_refresh"]["stale"] is False


def test_issued_today_needs_a_source_issued_date(env):
    """A permit marked issued but with no issued date is not 'issued today'
    just because it was ingested or updated today."""
    c = env["conn"]
    _permit(c, env["co1"], "NODATE-ISSUED", issued=None, filed=None, opp=None, lifecycle="Permit Issued")
    assert crm_admin.admin_dashboard(c, env["admin"])["kpis"]["new_issued_permits"] == 0
    _permit(c, env["co1"], "TODAY-ISSUED", issued=datetime.now(UTC).strftime("%Y-%m-%d"),
            lifecycle="Permit Issued")
    assert crm_admin.admin_dashboard(c, env["admin"])["kpis"]["new_issued_permits"] == 1


def test_admin_freshness_without_a_summary_never_defaults_to_current(env):
    c = env["conn"]
    c.execute("UPDATE jurisdictions SET last_sync_status='success', last_synced_at=?", (_ts(1),))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status, last_sync_status) "
              "VALUES ('mesa_az','Mesa','AZ','connected', NULL)")
    c.commit()
    _permit(c, env["co1"], "MESA-50", issued=_day(50), opp=_day(50), jurisdiction="mesa_az")
    # A refresh is running: there is no summary to read freshness from.
    _run(c, "running", None, started_hours_ago=0.1)
    rows = {r["slug"]: r for r in crm_admin.admin_dashboard(c, env["admin"])["jurisdiction_freshness"]}
    assert rows["mesa_az"]["status"] == "Stale" and rows["mesa_az"]["outreach_blocked"] is True
    assert rows["phoenix_az"]["status"] == "No data"  # synced "success", but no records at all
    assert rows["phoenix_az"]["outreach_blocked"] is True
    assert all(r["status"] != "Current" for r in rows.values())


def test_admin_summary_freshness_is_cross_checked_with_the_trust_layer(env):
    c = env["conn"]
    summary = {"completed_at": _ts(1), "jurisdictions": [
        {"slug": "phoenix_az", "name": "Phoenix", "status": "Current", "newest_source_date": _day(0),
         "records_received_today": 3}]}
    _run(c, "succeeded", 1, summary=summary)
    rows = crm_admin.admin_dashboard(c, env["admin"])["jurisdiction_freshness"]
    # The summary says Current, but the database holds no Phoenix permits: the
    # daily queue blocks it, and the panel says so.
    assert rows[0]["status"] == "Current" and rows[0]["outreach_blocked"] is True


# ------------------------------------------------------------ CEO brief


def test_brief_matching_an_old_refresh_is_not_current(tmp_path):
    _write(tmp_path, _brief(run_id=4))
    conn = _brief_conn()
    conn.execute("INSERT INTO pipeline_runs (id, run_type, status, completed_at) "
                 "VALUES (4, 'morning_refresh', 'succeeded', '2026-10-05T03:01:00')")
    fresh = load_owner_brief_view(reports_conn=conn, directory=tmp_path,
                                  now=datetime(2026, 10, 5, 20, tzinfo=UTC))
    assert fresh["current"] is True
    stale = load_owner_brief_view(reports_conn=conn, directory=tmp_path,
                                  now=datetime(2026, 10, 9, 8, tzinfo=UTC))
    assert stale["current"] is False
    assert "more than 36 hours ago" in stale["notice"] and "may be out of date" in stale["notice"]


def test_running_refresh_is_named_in_the_notice(tmp_path):
    _write(tmp_path, _brief(run_id=4))
    conn = _brief_conn()
    conn.execute("INSERT INTO pipeline_runs (id, run_type, status, completed_at) "
                 "VALUES (4, 'morning_refresh', 'succeeded', '2026-10-05T03:01:00')")
    conn.execute("INSERT INTO pipeline_runs (id, run_type, status, completed_at) "
                 "VALUES (5, 'morning_refresh', 'running', NULL)")
    view = load_owner_brief_view(reports_conn=conn, directory=tmp_path,
                                 now=datetime(2026, 10, 5, 20, tzinfo=UTC))
    assert view["current"] is False
    assert "A refresh is in progress" in view["notice"] and "None" not in view["notice"]


def _health_record(checked_at: str, status="healthy") -> dict:
    return {
        "schema": "corridoriq.ceo.health.v1", "overall_status": status, "label": "x",
        "checked_at": checked_at, "data_as_of": checked_at[:10], "refresh_run_id": 4,
        "scope": {"kind": "owner", "organization_id": None}, "execute": False,
        "checks": [], "incidents": [], "owner_alert_required": False, "components": {},
    }


def test_old_healthy_health_record_is_not_shown_as_healthy(tmp_path):
    (tmp_path / "latest_health_status.json").write_text(
        json.dumps(_health_record("2026-10-05T04:00:00+00:00")), encoding="utf-8")
    conn = _brief_conn()
    recent = resolve_health(tmp_path, conn, now=datetime(2026, 10, 5, 12, tzinfo=UTC))
    assert recent["overall_status"] == "healthy" and recent["current"] is True
    old = resolve_health(tmp_path, conn, now=datetime(2026, 10, 9, 12, tzinfo=UTC))
    assert old["overall_status"] == "unknown" and old["recorded_overall_status"] == "healthy"
    assert old["label"] != "ALL SYSTEMS HEALTHY"
    assert old["current"] is False and "more than 36 hours ago" in old["artifact_notice"]


def test_ceo_top_accounts_are_not_presented_as_buying_intent():
    rows = [
        {"company_id": 1, "company_name": "Old Plumbing", "account_priority_score": 99,
         "most_recent_relevant_date": "2026-06-01"},
        {"company_id": 2, "company_name": "Busy Plumbing", "account_priority_score": 80,
         "most_recent_relevant_date": "2026-10-01"},
        {"company_id": 3, "company_name": "Undated Plumbing", "account_priority_score": 70,
         "most_recent_relevant_date": None},
    ]
    text = _top_accounts(rows, "2026-10-05")
    assert "not current buying intent" in text
    assert "Old Plumbing" in text and "outside the 30-day window" in text.split("Old Plumbing")[1].split("\n")[0]
    assert "(4 days ago)" in text.split("Busy Plumbing")[1].split("\n")[0]
    assert "no dated relevant activity" in text.split("Undated Plumbing")[1]


# ------------------------------------------------------------ reports


def test_reports_older_than_the_latest_refresh_are_labelled(env, tmp_path, monkeypatch):
    c = env["conn"]
    gen = tmp_path / "generated"
    gen.mkdir()
    monkeypatch.setattr(reports_catalog.settings, "REPORTS_GENERATED_DIR", gen)
    old, new = gen / "opportunity_old.md", gen / "opportunity_new.md"
    old.write_text("old", encoding="utf-8")
    new.write_text("new", encoding="utf-8")
    t_old = (datetime.now(UTC) - timedelta(days=5)).timestamp()
    os.utime(old, (t_old, t_old))
    _run(c, "succeeded", 24)
    files = {f["name"]: f for f in reports_catalog.catalog(c, env["admin"])["files"]}
    assert files["opportunity_old.md"]["predates_latest_refresh"] is True
    assert "before the latest data refresh" in files["opportunity_old.md"]["freshness_note"]
    assert files["opportunity_new.md"]["predates_latest_refresh"] is False
    assert old.exists()  # labelled, never deleted
