"""Whole-pipeline orchestration + run tracking (Sprint 6.3).

The automated *morning refresh* runs the full chain in order, isolates
per-jurisdiction failures, records exactly what happened in ``pipeline_runs``,
and never blocks a second overlapping run. It reuses the existing ingestion,
analysis, company-intelligence, and export stages — it changes NO scoring
weights and NO 60-point publish threshold.

    python -m pipeline.run morning_refresh          # CLI / scheduler
    run_morning_refresh(conn)                        # in-process (API trigger)
"""

from __future__ import annotations

import json
import sqlite3
import time as _time
from datetime import datetime, timedelta, timezone

from pipeline.config import settings

RUN_TYPE = settings.MORNING_REFRESH_RUN_TYPE

# Lifecycle buckets used only for reporting counts (not scoring).
_SUBMITTED_STAGES = ("Application Submitted", "Plan Review")
_ISSUED_STAGE = "Permit Issued"
_ESTIMATOR_STAGES = ("Permit Issued", "Construction Active")


class RunInProgressError(Exception):
    """Raised when a morning refresh is already running (duplicate prevention)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _parse_iso(value: str | None):
    if not value:
        return None
    try:
        s = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        # Bare date like '2026-07-20'.
        try:
            return datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None


# ---------------------------------------------------------------------------
# Duplicate-run prevention (cross-process, via the pipeline_runs table).
# ---------------------------------------------------------------------------

def _reap_stale_runs(conn: sqlite3.Connection) -> None:
    """Fail any 'running' row left behind by a crashed process so it stops
    blocking new runs."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(minutes=settings.MORNING_RUN_STALE_MINUTES)).isoformat(timespec="seconds")
    conn.execute(
        "UPDATE pipeline_runs SET status='failed', completed_at=?, "
        "errors_json=COALESCE(errors_json, ?) "
        "WHERE run_type=? AND status='running' AND started_at < ?",
        (_now(), json.dumps([{"stage": "run", "error": "abandoned (stale run reaped)"}]),
         RUN_TYPE, cutoff),
    )
    conn.commit()


def _start_run(conn: sqlite3.Connection, trigger_source: str, triggered_by: int | None) -> int:
    """Atomically refuse a duplicate and claim a new 'running' row."""
    _reap_stale_runs(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT id FROM pipeline_runs WHERE run_type=? AND status='running'", (RUN_TYPE,)
        ).fetchone()
        if existing is not None:
            conn.rollback()
            raise RunInProgressError(
                f"A {RUN_TYPE} run (id={existing['id']}) is already in progress.")
        now = _now()
        cur = conn.execute(
            "INSERT INTO pipeline_runs (run_type, trigger_source, triggered_by, "
            "started_at, status, created_at) VALUES (?, ?, ?, ?, 'running', ?)",
            (RUN_TYPE, trigger_source, triggered_by, now, now),
        )
        run_id = cur.lastrowid
        conn.commit()
        return run_id
    except sqlite3.OperationalError as exc:
        # Could not get the reserved lock in time -> assume another run holds it.
        try:
            conn.rollback()
        except sqlite3.OperationalError:
            pass
        raise RunInProgressError(f"Could not acquire pipeline lock: {exc}") from exc


# ---------------------------------------------------------------------------
# Freshness classification.
# ---------------------------------------------------------------------------

def _freshness_status(newest_source_date: str | None, failed: bool) -> str:
    if failed:
        return "Failed"
    dt = _parse_iso(newest_source_date)
    if dt is None:
        return "Stale"
    days = (datetime.now(timezone.utc) - dt).days
    if days <= settings.MORNING_FRESHNESS_CURRENT_DAYS:
        return "Current"
    if days <= settings.MORNING_FRESHNESS_DELAYED_DAYS:
        return "Delayed"
    return "Stale"


def _days_since(newest_source_date: str | None):
    dt = _parse_iso(newest_source_date)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).days


# ---------------------------------------------------------------------------
# The orchestrated morning refresh.
# ---------------------------------------------------------------------------

def run_morning_refresh(
    conn: sqlite3.Connection | None = None,
    *,
    trigger_source: str = "cli",
    triggered_by: int | None = None,
    jurisdictions: list[str] | None = None,
    sleep=_time.sleep,
    verbose: bool = False,
) -> dict:
    """Execute the full morning refresh. Returns the run summary dict.

    Raises ``RunInProgressError`` if another run is already active. All other
    failures are captured into the run record rather than raised, so the
    scheduler always gets a clean exit path and a recorded status.
    """
    own_conn = conn is None
    if own_conn:
        from pipeline.db.database import init_db
        conn = init_db()

    def log(msg: str) -> None:
        if verbose:
            print(msg)

    stage_times: dict[str, float] = {}
    errors: list[dict] = []

    def _stage(name: str, fn):
        t0 = _time.time()
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - isolate stage failures
            errors.append({"stage": name, "error": f"{type(exc).__name__}: {exc}"})
            log(f"[{name}] ERROR {exc}")
            return None
        finally:
            stage_times[name] = round(_time.time() - t0, 2)

    run_started_at = _now()

    # 1. Database health check (also claims the run lock).
    _stage("db_health_check", lambda: conn.execute("PRAGMA quick_check").fetchone())
    if any(e["stage"] == "db_health_check" for e in errors):
        if own_conn:
            conn.close()
        raise RuntimeError("Database health check failed; aborting morning refresh.")

    run_id = _start_run(conn, trigger_source, triggered_by)
    log(f"[morning_refresh] run id={run_id} started {run_started_at}")

    from pipeline.ingestion.run_ingestion import ingest_jurisdiction

    # Which jurisdictions to attempt.
    q = ("SELECT slug, connector_type, last_synced_at FROM jurisdictions "
         "WHERE status = 'connected'")
    rows = [dict(r) for r in conn.execute(q).fetchall()]
    if jurisdictions:
        wanted = set(jurisdictions)
        rows = [r for r in rows if r["slug"] in wanted]

    # 2. Jurisdiction ingestion (retry + isolation), 3. normalization/lifecycle
    #    happen inside the analysis step below.
    per_jurisdiction: list[dict] = []

    def _do_ingest():
        for r in rows:
            st = ingest_jurisdiction(conn, r["slug"], r["connector_type"],
                                     r["last_synced_at"], sleep=sleep)
            per_jurisdiction.append(st)
            log(f"[{r['slug']}] {st['status']} fetched={st['fetched']} "
                f"new={st['inserted']} upd={st['updated']} unch={st['unchanged']}")
    _stage("ingestion", _do_ingest)

    # Touched = new or genuinely changed permits (unchanged rows never bumped).
    touched_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM permits WHERE last_updated_at >= ? OR first_seen_at >= ?",
        (run_started_at, run_started_at),
    ).fetchall()]

    # 4-6. Incremental analysis: normalize + score + lifecycle for changed rows
    #      only (never a full-dataset recompute).
    _stage("analysis", lambda: _run_incremental_analysis(conn, touched_ids))

    # 7. Company intelligence refresh (links + metrics + timeline + export).
    _stage("company_intelligence", lambda: _refresh_company_intelligence(conn))

    # 8-11. Work queues + dashboard/export refresh (submitted, issued, active,
    #       estimator queues are all served from these exports).
    _stage("exports", lambda: _refresh_exports(conn))

    # 12. Morning validation summary (freshness + counts).
    counts = _compute_counts(conn, per_jurisdiction, touched_ids, run_started_at)
    freshness = _compute_freshness(conn, per_jurisdiction)

    completed_at = _now()
    duration = round((_parse_iso(completed_at) - _parse_iso(run_started_at)).total_seconds(), 1)

    attempted = len(per_jurisdiction)
    succeeded = sum(1 for j in per_jurisdiction if j["status"] in ("success", "success_with_errors"))
    failed = attempted - succeeded

    status = _overall_status(attempted, succeeded, failed, errors)

    summary = {
        "run_id": run_id,
        "run_type": RUN_TYPE,
        "trigger_source": trigger_source,
        "status": status,
        "started_at": run_started_at,
        "completed_at": completed_at,
        "duration_seconds": duration,
        "jurisdictions_attempted": attempted,
        "jurisdictions_succeeded": succeeded,
        "jurisdictions_failed": failed,
        "records_received": counts["records_received"],
        "records_created": counts["records_created"],
        "records_updated": counts["records_updated"],
        "records_unchanged": counts["records_unchanged"],
        "submitted_only_records": counts["submitted_only_records"],
        "new_submitted_opportunities": counts["new_submitted_opportunities"],
        "new_issued_permits": counts["new_issued_permits"],
        "updated_projects": counts["updated_projects"],
        "estimator_ready": counts["estimator_ready"],
        "stage_seconds": stage_times,
        "jurisdictions": freshness,
        "errors": errors,
    }

    # Persist the run record.
    conn.execute(
        """
        UPDATE pipeline_runs SET
            completed_at=?, status=?,
            jurisdictions_attempted=?, jurisdictions_succeeded=?, jurisdictions_failed=?,
            records_received=?, records_created=?, records_updated=?, records_unchanged=?,
            submitted_only_records=?, errors_json=?, summary_json=?
        WHERE id=?
        """,
        (completed_at, status, attempted, succeeded, failed,
         counts["records_received"], counts["records_created"], counts["records_updated"],
         counts["records_unchanged"], counts["submitted_only_records"],
         json.dumps(errors), json.dumps(summary, default=str), run_id),
    )
    conn.commit()

    # Source health telemetry (Phase 2). Evaluated after every jurisdiction has
    # been attempted so the snapshot reflects a complete round. Guarded: a
    # reporting failure must not fail an otherwise successful refresh.
    from pipeline.telemetry.source_health import record_snapshot_safe

    for health in record_snapshot_safe(conn):
        if health["health_state"] not in ("healthy", "unknown"):
            log(f"[health] {health['jurisdiction_slug']}: "
                f"{health['health_state'].upper()} - {health['detail']}")

    # Notifications + dashboard feed + timestamped log.
    _write_summary_markdown(summary)
    _export_status_json(conn)
    _write_log(summary)

    log(f"[morning_refresh] status={status} in {duration}s")
    if own_conn:
        conn.close()
    return summary


# ---------------------------------------------------------------------------
# Stage implementations (thin wrappers over existing pipeline code).
# ---------------------------------------------------------------------------

def _run_incremental_analysis(conn: sqlite3.Connection, touched_ids: list[int]) -> int:
    from pipeline.analysis.run_analysis import run_analysis_for_permits
    return run_analysis_for_permits(conn, touched_ids)


def _refresh_company_intelligence(conn: sqlite3.Connection) -> None:
    from pipeline.company_resolution.backfill import backfill_companies
    from pipeline.company_resolution.export import export_all as export_companies
    from pipeline.company_resolution.metrics import compute_company_metrics
    from pipeline.company_resolution.timeline import rebuild_company_timeline

    backfill_companies(conn, resume=True)
    compute_company_metrics(conn)
    rebuild_company_timeline(conn)
    from pipeline.contractor_intel.classify import classify_companies
    from pipeline.relevance.classify import score_project_relevance

    classify_companies(conn)
    score_project_relevance(conn)
    from pipeline.relevance.account import score_company_accounts

    score_company_accounts(conn)
    export_companies(conn)


def _refresh_exports(conn: sqlite3.Connection) -> None:
    from pipeline.export.export_json import export_all
    export_all(conn)


# ---------------------------------------------------------------------------
# Metric + freshness computation.
# ---------------------------------------------------------------------------

def _compute_counts(conn, per_jurisdiction, touched_ids, run_started_at) -> dict:
    received = sum(j["fetched"] for j in per_jurisdiction)
    created = sum(j["inserted"] for j in per_jurisdiction)
    updated = sum(j["updated"] for j in per_jurisdiction)
    unchanged = sum(j["unchanged"] for j in per_jurisdiction)

    new_submitted = new_issued = submitted_only = estimator_ready = 0
    if touched_ids:
        rows = []
        for start in range(0, len(touched_ids), 500):
            chunk = touched_ids[start:start + 500]
            ph = ",".join("?" for _ in chunk)
            rows.extend(conn.execute(
                f"""
                SELECT pr.project_lifecycle AS lifecycle, pr.opportunity_score AS score,
                       p.issued_date AS issued_date
                FROM permits p JOIN projects pr ON pr.permit_id = p.id
                WHERE p.id IN ({ph})
                """, chunk,
            ).fetchall())
        threshold = settings.REPORT_MIN_OPPORTUNITY_SCORE
        for r in rows:
            lifecycle = r["lifecycle"]
            score = r["score"] or 0
            if lifecycle in _SUBMITTED_STAGES and not r["issued_date"]:
                submitted_only += 1
                if score >= threshold:
                    new_submitted += 1
            if lifecycle == _ISSUED_STAGE:
                new_issued += 1
            if lifecycle in _ESTIMATOR_STAGES and score >= threshold:
                estimator_ready += 1

    return {
        "records_received": received,
        "records_created": created,
        "records_updated": updated,
        "records_unchanged": unchanged,
        "submitted_only_records": submitted_only,
        "new_submitted_opportunities": new_submitted,
        "new_issued_permits": new_issued,
        "updated_projects": updated,
        "estimator_ready": estimator_ready,
    }


def _compute_freshness(conn, per_jurisdiction) -> list[dict]:
    by_slug = {j["slug"]: j for j in per_jurisdiction}
    out = []
    for slug, j in by_slug.items():
        name_row = conn.execute(
            "SELECT name, last_synced_at, last_sync_status FROM jurisdictions WHERE slug=?",
            (slug,),
        ).fetchone()
        failed = j["status"] == "failed" or (name_row and name_row["last_sync_status"] == "error")
        newest = j.get("newest_source_date")
        out.append({
            "slug": slug,
            "name": name_row["name"] if name_row else slug,
            "status": _freshness_status(newest, bool(failed)),
            "last_successful_refresh": name_row["last_synced_at"] if name_row and not failed else None,
            "newest_submitted_date": j.get("newest_submitted_date"),
            "newest_issued_date": j.get("newest_issued_date"),
            "newest_source_date": newest,
            "records_received_today": j.get("fetched", 0),
            "days_since_newest": _days_since(newest),
            "error": j.get("error"),
        })
    out.sort(key=lambda r: (r["status"] != "Failed", r["name"]))
    return out


def _overall_status(attempted, succeeded, failed, errors) -> str:
    stage_failed = any(e["stage"] in ("analysis", "exports") for e in errors)
    if attempted == 0:
        return "failed" if errors else "succeeded"
    if succeeded == 0:
        return "failed"
    if failed > 0 or errors:
        return "partial"
    if stage_failed:
        return "partial"
    return "succeeded"


# ---------------------------------------------------------------------------
# Notifications / feeds / logs.
# ---------------------------------------------------------------------------

def _write_summary_markdown(summary: dict) -> None:
    settings.REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = settings.REPORTS_GENERATED_DIR / f"morning_refresh_summary_{_today()}.md"
    js = summary["jurisdictions"]
    delayed = [j for j in js if j["status"] in ("Delayed", "Stale", "Failed")]
    lines = [
        f"# Morning refresh — {summary['status'].upper()}",
        "",
        f"- Run id: {summary['run_id']}  ({summary['trigger_source']})",
        f"- Completed: {summary['completed_at']}  in {summary['duration_seconds']}s",
        f"- {summary['jurisdictions_attempted']} jurisdictions attempted",
        f"- {summary['jurisdictions_succeeded']} succeeded",
        f"- {summary['jurisdictions_failed']} failed",
        f"- {summary['new_submitted_opportunities']} new submitted opportunities",
        f"- {summary['new_issued_permits']} new issued permits",
        f"- {summary['updated_projects']} existing projects updated",
        f"- {summary['estimator_ready']} estimator assignments ready",
        f"- Records: received {summary['records_received']}, "
        f"created {summary['records_created']}, updated {summary['records_updated']}, "
        f"unchanged {summary['records_unchanged']}",
        "",
        "## Jurisdiction freshness",
        "",
        "| Jurisdiction | Status | Newest source | Received today |",
        "| --- | --- | --- | --- |",
    ]
    for j in js:
        lines.append(f"| {j['name']} | {j['status']} | {j['newest_source_date'] or '—'} "
                     f"| {j['records_received_today']} |")
    if delayed:
        lines += ["", "## Attention", ""]
        for j in delayed:
            note = j["error"] or f"{j['days_since_newest']} days since newest record"
            lines.append(f"- **{j['name']}** ({j['status']}): {note}")
    if summary["errors"]:
        lines += ["", "## Errors", ""]
        for e in summary["errors"]:
            lines.append(f"- [{e['stage']}] {e['error']}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _export_status_json(conn: sqlite3.Connection) -> None:
    """Static dashboard feed (mirrors the API for the fallback path)."""
    settings.DATA_EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    payload = admin_status(conn)
    (settings.DATA_EXPORTS_DIR / "morning_refresh.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8")


def _write_log(summary: dict) -> None:
    settings.MORNING_REFRESH_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = settings.MORNING_REFRESH_LOG_DIR / f"morning_refresh_{ts}.log"
    path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")


# ---------------------------------------------------------------------------
# Status readers (used by the API + dashboard).
# ---------------------------------------------------------------------------

def _row_to_run(row) -> dict:
    d = dict(row)
    for key in ("errors_json", "summary_json"):
        raw = d.pop(key, None)
        d[key.replace("_json", "")] = json.loads(raw) if raw else None
    return d


def latest_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM pipeline_runs WHERE run_type=? ORDER BY started_at DESC, id DESC LIMIT 1",
        (RUN_TYPE,),
    ).fetchone()
    return _row_to_run(row) if row else None


def is_running(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pipeline_runs WHERE run_type=? AND status='running' LIMIT 1", (RUN_TYPE,)
    ).fetchone()
    return row is not None


def run_history(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT id, trigger_source, started_at, completed_at, status, "
        "jurisdictions_attempted, jurisdictions_succeeded, jurisdictions_failed, "
        "records_received, records_created, records_updated "
        "FROM pipeline_runs WHERE run_type=? ORDER BY started_at DESC, id DESC LIMIT ?",
        (RUN_TYPE, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


_EMPLOYEE_LABELS = {
    "succeeded": "Data current",
    "partial": "Refresh partially delayed",
    "failed": "Data refresh failed",
    "running": "Refresh in progress",
}


def employee_status(conn: sqlite3.Connection) -> dict:
    """Simple, non-technical status for sales/estimator users."""
    run = latest_run(conn)
    if run is None:
        return {"label": "No refresh yet", "status": None, "last_completed": None}
    return {
        "label": _EMPLOYEE_LABELS.get(run["status"], run["status"]),
        "status": run["status"],
        "last_completed": run["completed_at"],
    }


def admin_status(conn: sqlite3.Connection) -> dict:
    """Detailed status (errors, freshness, history) for admins/monitors."""
    run = latest_run(conn)
    summary = run.get("summary") if run else None
    return {
        "running": is_running(conn),
        "latest": run,
        "summary": summary,
        "freshness": (summary or {}).get("jurisdictions") if summary else None,
        "history": run_history(conn, limit=10),
    }
