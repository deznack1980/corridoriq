"""Known-answer tests for governed CEO analytics. Facts are fixed; prose is not."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from agents.ceo.analytics.answer import answer_question
from agents.ceo.analytics.brief import SCHEDULING_ENABLED, build_daily_brief, schedule, write_brief
from agents.ceo.analytics.guard import AnalyticsError, open_analytics, run_select
from agents.ceo.analytics.limits import MAX_ROWS
from agents.ceo.analytics.questions import CANONICAL_QUESTIONS
from agents.ceo.analytics.scope import organization_scope, owner_scope
from agents.ceo.analytics.tools import _clamp, get_top_opportunities
from agents.ceo.analytics.windows import require_window
from agents.ceo.cli import main

AS_OF = "2026-10-05"


def _build(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE companies (id INTEGER PRIMARY KEY, display_name TEXT);
        CREATE TABLE permits (
            id INTEGER PRIMARY KEY, jurisdiction TEXT, permit_number TEXT, issued_date TEXT,
            city TEXT, description TEXT, first_seen_at TEXT, contractor_company_id INTEGER
        );
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY, permit_id INTEGER, contractor_company_id INTEGER
        );
        CREATE TABLE company_customer_priority (
            company_id INTEGER, profile_key TEXT, account_priority_score REAL,
            trade_identity TEXT, relevant_30d INTEGER, most_recent_relevant_date TEXT,
            why_now TEXT, primary_demand_category TEXT, model_version TEXT, generated_at TEXT
        );
        CREATE TABLE company_contact_channels (
            company_id INTEGER, contact_type TEXT, verification_status TEXT,
            status TEXT, source_family TEXT
        );
        CREATE TABLE source_health_snapshot (
            jurisdiction_slug TEXT, health_state TEXT, newest_source_date TEXT,
            days_since_newest_source REAL, records_7d INTEGER, captured_at TEXT, last_success_at TEXT
        );
        CREATE TABLE pipeline_runs (
            id INTEGER PRIMARY KEY, run_type TEXT, status TEXT, started_at TEXT, completed_at TEXT,
            jurisdictions_attempted INTEGER, jurisdictions_succeeded INTEGER,
            jurisdictions_failed INTEGER, records_received INTEGER
        );
        CREATE TABLE sales_lane_snapshots (
            lane_key TEXT, presentation_rank INTEGER, primary_company_id INTEGER,
            canonical_name TEXT, model_version TEXT, created_at TEXT
        );
        CREATE TABLE organizations (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE crm_company_relationships (
            organization_id INTEGER, company_id INTEGER, relationship_status TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO companies VALUES (?,?)",
        [(1, "Alpha Plumbing"), (2, "Beta Plumbing"), (3, "Gamma Plumbing"), (4, "Quiet Co")],
    )
    conn.executemany(
        "INSERT INTO company_customer_priority VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (1, "plumbing_supply", 82, "plumbing_specialist", 0, "2026-05-01", "stored why now lags", "plumbing_service", "v1", "2026-10-01"),
            (2, "plumbing_supply", 91, "plumbing_specialist", 0, None, "no recent work", "plumbing_fixture", "v1", "2026-10-01"),
            (3, "plumbing_supply", 75, "recurring_plumbing", 0, "2026-10-02", "mesa work", "plumbing_service", "v1", "2026-10-01"),
            (4, "plumbing_supply", 40, "other_trade", 0, None, "not plumbing", "unknown", "v1", "2026-10-01"),
        ],
    )
    permits = [
        (1, "phoenix_az", "P1", "2026-08-20", "Phoenix", "water heater", "2026-08-20T00:00:00", 1),
        (2, "phoenix_az", "P2", "2026-09-20", "Phoenix", "repipe", "2026-09-20T00:00:00", 1),
        (3, "phoenix_az", "P3", "2026-09-25", "Phoenix", "fixture", "2026-09-25T00:00:00", 1),
        (4, "phoenix_az", "P4", "2026-10-01", "Phoenix", "water line", "2026-10-04T12:00:00", 1),
        (5, "mesa_az", "M1", "2026-09-28", "Mesa", "gas line", "2026-09-28T00:00:00", 3),
        (6, "mesa_az", "M2", "2026-10-02", "Mesa", "water heater", "2026-10-02T00:00:00", 3),
    ]
    conn.executemany("INSERT INTO permits VALUES (?,?,?,?,?,?,?,?)", permits)
    conn.executemany(
        "INSERT INTO projects VALUES (?,?,?)",
        [(10, 1, 1), (11, 2, 1), (12, 3, 1), (13, 4, 1), (14, 5, 3), (15, 6, 3)],
    )
    conn.executemany(
        "INSERT INTO company_contact_channels VALUES (?,?,?,?,?)",
        [
            (1, "business_phone", "VERIFIED", "active", "prior_research"),
            (2, "business_phone", "VERIFIED", "active", "canonical_peer"),
            (3, "business_email", "VERIFIED", "active", "official_website"),
        ],
    )
    conn.executemany(
        "INSERT INTO source_health_snapshot VALUES (?,?,?,?,?,?,?)",
        [
            ("phoenix_az", "healthy", "2026-10-03", 2, 12, "2026-10-04T03:00:00", "2026-10-04T03:00:00"),
            ("chandler_az", "healthy", "2025-08-01", 400, 0, "2026-10-04T03:00:00", "2026-10-04T03:00:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO pipeline_runs VALUES (?,?,?,?,?,?,?,?,?)",
        [
            (1, "morning_refresh", "succeeded", "2026-10-03T03:00:00", "2026-10-03T03:01:00", 9, 9, 0, 40),
            (2, "morning_refresh", "succeeded", "2026-10-04T03:00:00", "2026-10-04T03:01:00", 9, 9, 0, 10),
        ],
    )
    conn.executemany(
        "INSERT INTO sales_lane_snapshots VALUES (?,?,?,?,?,?)",
        [
            ("PLUMBING_CORE", 1, 2, "Beta Plumbing", "snap-a", "2026-09-01"),
            ("PLUMBING_CORE", 2, 1, "Alpha Plumbing", "snap-a", "2026-09-01"),
            ("PLUMBING_CORE", 1, 1, "Alpha Plumbing", "snap-b", "2026-10-01"),
            ("PLUMBING_CORE", 2, 2, "Beta Plumbing", "snap-b", "2026-10-01"),
        ],
    )
    conn.executemany("INSERT INTO organizations VALUES (?,?)", [(1, "North"), (2, "South")])
    conn.executemany(
        "INSERT INTO crm_company_relationships VALUES (?,?,?)",
        [(1, 1, "customer"), (2, 2, "customer")],
    )
    conn.commit()
    conn.close()


@pytest.fixture()
def db(tmp_path):
    path = tmp_path / "intel.db"
    _build(path)
    return path


def _rows(payload, tool):
    for result in payload["results"]:
        if result["tool"] == tool:
            return result
    raise AssertionError(tool)


def test_canonical_question_count():
    assert 15 <= len(CANONICAL_QUESTIONS) <= 20


def test_readonly_and_authorizer_reject_writes(db):
    conn = open_analytics(db)
    with pytest.raises(sqlite3.Error):
        conn.execute("UPDATE companies SET display_name = 'x'")
    with pytest.raises(AnalyticsError):
        run_select(conn, "INSERT INTO companies VALUES (9, 'no')")
    conn.close()
    check = sqlite3.connect(db)
    assert check.execute("SELECT display_name FROM companies WHERE id=1").fetchone()[0] == "Alpha Plumbing"
    check.close()


def test_parameter_and_row_bounds():
    with pytest.raises(AnalyticsError):
        require_window(365)
    assert _clamp(1000) == MAX_ROWS
    assert _clamp(3) == 3


def test_known_answers(db):
    owner = owner_scope()

    changed = answer_question(CANONICAL_QUESTIONS[0], db_path=db, as_of=AS_OF, scope=owner)
    assert changed["status"] == "KNOWN"
    assert _rows(changed, "get_recent_changes")["new_permit_count"] == 1

    yesterday = answer_question(CANONICAL_QUESTIONS[1], db_path=db, as_of=AS_OF, scope=owner)
    day = _rows(yesterday, "compare_activity_periods")
    assert day["window_days"] == 1
    assert day["current_total"] == 0

    top = answer_question(CANONICAL_QUESTIONS[2], db_path=db, as_of=AS_OF, scope=owner)
    names = [row["company_name"] for row in _rows(top, "get_top_opportunities")["rows"]]
    assert names == ["Beta Plumbing", "Alpha Plumbing", "Gamma Plumbing"]

    increase = answer_question(CANONICAL_QUESTIONS[3], db_path=db, as_of=AS_OF, scope=owner)
    deltas = _rows(increase, "compare_activity_periods")["rows"]
    assert deltas[0]["company_name"] == "Alpha Plumbing"
    assert deltas[0]["current_permits"] == 3
    assert deltas[0]["prior_permits"] == 1
    assert deltas[0]["delta"] == 2
    assert deltas[1]["company_name"] == "Gamma Plumbing"
    assert deltas[1]["delta"] == 2

    multiple = answer_question(CANONICAL_QUESTIONS[4], db_path=db, as_of=AS_OF, scope=owner)
    multi = [row for row in _rows(multiple, "get_company_activity")["rows"] if row["observed_permits"] >= 2]
    assert [row["company_id"] for row in multi] == [1, 3]

    gaps = answer_question(CANONICAL_QUESTIONS[5], db_path=db, as_of=AS_OF, scope=owner)
    gap_rows = _rows(gaps, "get_contact_gaps")
    assert [row["company_id"] for row in gap_rows["rows"]] == [2]
    assert gap_rows["usable_contact_pct"] == 66.7
    assert gap_rows["high_priority"] == 3
    assert gap_rows["with_usable_contact"] == 2

    why = answer_question(CANONICAL_QUESTIONS[6], db_path=db, as_of=AS_OF, scope=owner)
    evidence = _rows(why, "compare_activity_periods")["rows"][0]
    assert evidence["company_id"] == 1
    assert evidence["current_permits"] == 3
    assert any(claim["class"] == "INFERENCE" and "dollar" in claim["text"] for claim in why["claims"])

    cities = answer_question(CANONICAL_QUESTIONS[7], db_path=db, as_of=AS_OF, scope=owner)
    market = _rows(cities, "get_market_activity")["rows"]
    assert market[0]["city"] == "Phoenix" and market[0]["observed_permits"] == 3
    assert market[1]["city"] == "Mesa" and market[1]["observed_permits"] == 2

    compare = answer_question(CANONICAL_QUESTIONS[8], db_path=db, as_of=AS_OF, scope=owner)
    totals = _rows(compare, "compare_activity_periods")
    assert totals["current_total"] == 5
    assert totals["prior_total"] == 1
    assert totals["delta"] == 4

    ranking = answer_question(CANONICAL_QUESTIONS[9], db_path=db, as_of=AS_OF, scope=owner)
    ranks = {row["company_id"]: row for row in _rows(ranking, "get_ranking_changes")["rows"]}
    assert ranks[1]["prior_rank"] == 2 and ranks[1]["current_rank"] == 1
    assert ranks[2]["prior_rank"] == 1 and ranks[2]["current_rank"] == 2

    pct = answer_question(CANONICAL_QUESTIONS[10], db_path=db, as_of=AS_OF, scope=owner)
    assert _rows(pct, "get_contact_gaps")["usable_contact_pct"] == 66.7

    stale = answer_question(CANONICAL_QUESTIONS[11], db_path=db, as_of=AS_OF, scope=owner)
    fresh = {row["jurisdiction_slug"]: row for row in _rows(stale, "get_feed_freshness")["rows"]}
    assert fresh["chandler_az"]["stale"] is True
    assert fresh["phoenix_az"]["stale"] is False

    thin = answer_question(CANONICAL_QUESTIONS[12], db_path=db, as_of=AS_OF, scope=owner)
    assert _rows(thin, "get_feed_freshness")["rows"][0]["jurisdiction_slug"] in {"chandler_az", "phoenix_az"}
    chandler = next(row for row in _rows(thin, "get_feed_freshness")["rows"] if row["jurisdiction_slug"] == "chandler_az")
    assert chandler["thin"] is True

    pipeline = answer_question(CANONICAL_QUESTIONS[13], db_path=db, as_of=AS_OF, scope=owner)
    runs = _rows(pipeline, "get_pipeline_health")["rows"]
    assert runs[0]["records_received"] == 10
    assert runs[1]["records_received"] == 40
    assert runs[0]["status"] == "succeeded"

    missing_tenant = answer_question(CANONICAL_QUESTIONS[14], db_path=db, as_of=AS_OF, scope=owner)
    assert missing_tenant["status"] == "UNKNOWN"
    assert "tenant unresolved" in missing_tenant["claims"][0]["text"]

    org1 = answer_question(CANONICAL_QUESTIONS[14], db_path=db, as_of=AS_OF, scope=organization_scope(1))
    book = _rows(org1, "get_customer_book_activity")["rows"]
    assert [row["company_id"] for row in book] == [1]
    assert book[0]["observed_permits"] == 3

    org2 = answer_question(CANONICAL_QUESTIONS[14], db_path=db, as_of=AS_OF, scope=organization_scope(2))
    assert [row["company_id"] for row in _rows(org2, "get_customer_book_activity")["rows"]] == [2]

    week = answer_question(CANONICAL_QUESTIONS[15], db_path=db, as_of=AS_OF, scope=owner)
    assert _rows(week, "compare_activity_periods")["window_days"] == 7
    assert any(claim["class"] == "RECOMMENDATION" and "Alpha Plumbing" in claim["text"] for claim in week["claims"])

    new = answer_question(CANONICAL_QUESTIONS[16], db_path=db, as_of=AS_OF, scope=owner)
    assert _rows(new, "get_recent_changes")["new_permit_count"] == 1
    assert _rows(new, "get_recent_changes")["rows"][0]["permit_id"] == 4

    overlap = answer_question(CANONICAL_QUESTIONS[17], db_path=db, as_of=AS_OF, scope=organization_scope(1))
    assert [row["company_id"] for row in _rows(overlap, "get_customer_book_activity")["rows"]] == [1]

    anomalies = answer_question(CANONICAL_QUESTIONS[18], db_path=db, as_of=AS_OF, scope=owner)
    assert any(row["stale"] for row in _rows(anomalies, "get_feed_freshness")["rows"])

    today = answer_question(CANONICAL_QUESTIONS[19], db_path=db, as_of=AS_OF, scope=owner)
    assert today["execute"] is False
    assert any("Alpha Plumbing" in claim["text"] for claim in today["claims"] if claim["class"] == "RECOMMENDATION")


def test_fail_closed(db, tmp_path, monkeypatch):
    missing = answer_question("What changed since yesterday?", db_path=tmp_path / "absent.db", as_of=AS_OF)
    assert missing["status"] == "UNKNOWN"
    unsupported = answer_question("How many dollars of pipe does Alpha need?", db_path=db, as_of=AS_OF)
    assert unsupported["status"] == "UNSUPPORTED"
    assert unsupported["execute"] is False

    monkeypatch.setenv("CEO_ENABLE_MODEL_NARRATION", "1")
    monkeypatch.delenv("CEO_MODEL_API_KEY", raising=False)
    narrated = answer_question("What changed since yesterday?", db_path=db, as_of=AS_OF)
    assert narrated["status"] == "KNOWN"
    assert narrated["model"] == "unavailable"
    assert narrated["results"][0]["current_total"] == 0

    def _bad(*_args, **_kwargs):
        return "not-a-dict"

    monkeypatch.setattr("agents.ceo.analytics.answer.call_tool", _bad)
    malformed = answer_question("Which data feeds are stale?", db_path=db, as_of=AS_OF)
    assert malformed["status"] == "UNKNOWN"
    assert malformed["claims"][0]["text"] == "malformed tool result"


def test_daily_brief_json_and_markdown(db, tmp_path, monkeypatch):
    assert SCHEDULING_ENABLED is False
    with pytest.raises(RuntimeError):
        schedule()
    brief = build_daily_brief(db_path=db, as_of=AS_OF)
    assert brief["execute"] is False
    assert "Alpha Plumbing" in brief["markdown"]
    assert "chandler_az" in brief["markdown"]
    assert "INFERENCE" in brief["markdown"]
    assert brief["sections"]["activity_30d"]["delta"] == 4
    monkeypatch.setenv("CEO_OUTPUT_DIR", str(tmp_path))
    paths = write_brief(brief, tmp_path / "intelligence")
    assert Path(paths["json"]).exists()
    assert Path(paths["markdown"]).exists()
    assert main(["daily-brief", "--db", str(db), "--as-of", AS_OF, "--no-write"]) == 0
    assert main(["data", "Which high-priority opportunities lack usable contacts?", "--db", str(db), "--as-of", AS_OF]) == 0


def test_tool_limit_is_applied(db):
    conn = open_analytics(db)
    try:
        result = get_top_opportunities(conn, as_of=AS_OF, limit=1000)
    finally:
        conn.close()
    assert result["row_count"] <= MAX_ROWS
