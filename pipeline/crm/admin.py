"""Manager/admin operations: team overview and user management. All checks
enforce permissions and organization boundaries. Password hashes and security
counters are never serialized out."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline.auth.rbac import (
    CEO_AGENT_PERMISSION,
    OWNER_ROLE,
    AuthzError,
    ROLES,
    has_permission,
    require_permission,
)
from pipeline.auth.service import create_user as _create_user, write_audit
from pipeline.crm import serializers
from pipeline.crm.service import ValidationError

_OWNER_ROLE_MESSAGE = ("The owner role is assigned only by the server-side grant command "
                       "(python -m pipeline.auth.grant_owner).")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _roles_for(conn, user_id):
    return [r["name"] for r in conn.execute(
        "SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=?",
        (user_id,))]


def _guard_owner_account(conn, user: dict, target_id: int) -> set[str]:
    """The owner's account can be changed through the API only by the owner,
    so an ordinary administrator cannot take it over (e.g. by changing its
    email and resetting the password). Returns the target's current roles."""
    current = set(_roles_for(conn, target_id))
    if OWNER_ROLE in current and not has_permission(user, CEO_AGENT_PERMISSION):
        raise AuthzError("the owner account can only be changed by the owner")
    return current


def list_users(conn: sqlite3.Connection, user: dict) -> list[dict]:
    require_permission(user, "users.view")
    rows = conn.execute(
        "SELECT * FROM users WHERE organization_id=? ORDER BY display_name",
        (user["organization_id"],),
    ).fetchall()
    out = []
    for r in rows:
        d = serializers.serialize_user(r)
        d["roles"] = _roles_for(conn, r["id"])
        out.append(d)
    return out


def create_user(conn: sqlite3.Connection, user: dict, data: dict, *, ip=None, ua=None) -> dict:
    require_permission(user, "users.create")
    email = (data.get("email") or "").strip()
    roles = data.get("roles") or ["sales_representative"]
    for rn in roles:
        if rn not in ROLES:
            raise ValidationError(f"unknown role: {rn}")
    if OWNER_ROLE in roles:
        raise AuthzError(_OWNER_ROLE_MESSAGE)
    password = data.get("password")
    if not password:
        from pipeline.auth.passwords import generate_temp_password
        password = generate_temp_password()
    try:
        uid = _create_user(
            conn, organization_id=user["organization_id"], email=email,
            password=password, first_name=data.get("first_name", ""),
            last_name=data.get("last_name", ""), phone=data.get("phone"),
            role_names=roles, must_change_password=data.get("must_change_password", True),
            created_by=user["id"],
        )
    except ValueError as exc:
        raise ValidationError(str(exc))
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    result = serializers.serialize_user(row)
    result["roles"] = roles
    # Temp password returned once to the admin caller only (never persisted/logged).
    if not data.get("password"):
        result["temporary_password"] = password
    return result


def update_user(conn: sqlite3.Connection, user: dict, target_id: int, data: dict,
                *, ip=None, ua=None) -> dict:
    row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    if row is None or row["organization_id"] != user["organization_id"]:
        raise AuthzError("user not found", status=404)
    current_roles = _guard_owner_account(conn, user, target_id)
    updates = {}
    for k in ("first_name", "last_name", "display_name", "phone"):
        if k in data:
            require_permission(user, "users.update")
            updates[k] = data[k]
    if "email" in data:
        require_permission(user, "users.update")
        from pipeline.auth.service import apply_email_change
        try:
            apply_email_change(conn, target_id, data["email"], actor_id=user["id"],
                               ip=ip, ua=ua, commit=False)
        except ValueError as exc:
            raise ValidationError(str(exc))
    if "is_active" in data:
        require_permission(user, "users.disable")
        updates["is_active"] = 1 if data["is_active"] else 0
        if not updates["is_active"]:
            # Disabling revokes sessions immediately.
            from pipeline.auth.sessions import revoke_user_sessions
            revoke_user_sessions(conn, target_id)
            write_audit(conn, event_type="account_disabled", success=True,
                        user_id=user["id"], organization_id=user["organization_id"],
                        resource_type="user", resource_id=target_id, ip_address=ip,
                        user_agent=ua)
    if "roles" in data:
        require_permission(user, "roles.manage")
        for rn in data["roles"]:
            if rn not in ROLES:
                raise ValidationError(f"unknown role: {rn}")
        requested = set(data["roles"])
        # The owner role is neither granted nor removed here; an existing grant
        # survives any role edit made through the API.
        if OWNER_ROLE in requested and OWNER_ROLE not in current_roles:
            raise AuthzError(_OWNER_ROLE_MESSAGE)
        if OWNER_ROLE in current_roles:
            requested.add(OWNER_ROLE)
        conn.execute("DELETE FROM user_roles WHERE user_id=?", (target_id,))
        for rn in sorted(requested):
            rid = conn.execute("SELECT id FROM roles WHERE name=?", (rn,)).fetchone()["id"]
            conn.execute(
                "INSERT OR IGNORE INTO user_roles (user_id, role_id, assigned_by, assigned_at) "
                "VALUES (?,?,?,?)", (target_id, rid, user["id"], _now()))
    if updates:
        updates["updated_at"] = _now()
        set_sql = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE users SET {set_sql} WHERE id=?", [*updates.values(), target_id])
    conn.commit()
    write_audit(conn, event_type="admin_change", success=True, user_id=user["id"],
                organization_id=user["organization_id"], resource_type="user",
                resource_id=target_id, action="update_user", ip_address=ip, user_agent=ua)
    row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    out = serializers.serialize_user(row)
    out["roles"] = _roles_for(conn, target_id)
    return out


def team_overview(conn: sqlite3.Connection, user: dict) -> list[dict]:
    require_permission(user, "users.view")
    org_id = user["organization_id"]
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rows = conn.execute(
        "SELECT id, display_name, email FROM users WHERE organization_id=? AND is_active=1 "
        "ORDER BY display_name", (org_id,)).fetchall()

    def _n(sql, params):
        return conn.execute(sql, params).fetchone()["n"]

    out = []
    for r in rows:
        uid = r["id"]
        assigned = _n("SELECT COUNT(*) AS n FROM crm_company_relationships WHERE organization_id=? "
                      "AND assigned_user_id=?", (org_id, uid))
        open_tasks = _n("SELECT COUNT(*) AS n FROM crm_tasks WHERE organization_id=? "
                        "AND assigned_user_id=? AND status IN ('open','in_progress')", (org_id, uid))
        overdue_tasks = _n("SELECT COUNT(*) AS n FROM crm_tasks WHERE organization_id=? "
                           "AND assigned_user_id=? AND status IN ('open','in_progress') "
                           "AND due_at IS NOT NULL AND substr(due_at,1,10) < ?", (org_id, uid, today))
        activities = _n("SELECT COUNT(*) AS n FROM crm_activities WHERE organization_id=? "
                        "AND user_id=?", (org_id, uid))
        calls = _n("SELECT COUNT(*) AS n FROM crm_activities WHERE organization_id=? AND user_id=? "
                   "AND activity_type IN ('call','voicemail')", (org_id, uid))
        appointments = _n("SELECT COUNT(*) AS n FROM crm_activities WHERE organization_id=? "
                          "AND user_id=? AND activity_outcome='appointment_set'", (org_id, uid))
        quotes = _n("SELECT COUNT(*) AS n FROM crm_activities WHERE organization_id=? AND user_id=? "
                    "AND activity_type IN ('quote_request','quote_sent')", (org_id, uid))
        followups_due = _n("SELECT COUNT(*) AS n FROM crm_company_relationships WHERE organization_id=? "
                           "AND assigned_user_id=? AND next_followup_at IS NOT NULL "
                           "AND substr(next_followup_at,1,10) <= ?", (org_id, uid, today))
        no_activity = _n("SELECT COUNT(*) AS n FROM crm_company_relationships r WHERE r.organization_id=? "
                         "AND r.assigned_user_id=? AND NOT EXISTS (SELECT 1 FROM crm_activities a "
                         "WHERE a.company_id=r.company_id AND a.organization_id=r.organization_id)",
                         (org_id, uid))
        out.append({
            "user_id": uid, "display_name": r["display_name"], "email": r["email"],
            "roles": _roles_for(conn, uid), "assigned_companies": assigned,
            "open_tasks": open_tasks, "overdue_tasks": overdue_tasks,
            "activities_logged": activities, "calls_completed": calls,
            "appointments": appointments, "quote_requests": quotes,
            "followups_due": followups_due, "companies_no_activity": no_activity,
        })
    return out


def admin_dashboard(conn: sqlite3.Connection, user: dict) -> dict:
    """Organization-wide admin dashboard. Never assignment-scoped."""
    require_permission(user, "admin.system")
    from pipeline.config import settings
    from pipeline import pipeline_runs

    org_id = user["organization_id"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    threshold = getattr(settings, "REPORT_MIN_OPPORTUNITY_SCORE", 60)

    def _n(sql, params=()):
        return conn.execute(sql, params).fetchone()["n"]

    total_companies = _n("SELECT COUNT(*) AS n FROM companies WHERE lifecycle_state='active'")
    total_projects = _n("SELECT COUNT(*) AS n FROM projects")
    total_permits = _n("SELECT COUNT(*) AS n FROM permits")

    new_submitted = _n(
        "SELECT COUNT(*) AS n FROM projects pr JOIN permits p ON p.id=pr.permit_id "
        "WHERE pr.project_lifecycle IN ('Application Submitted','Plan Review') "
        "AND (p.issued_date IS NULL OR p.issued_date='') "
        "AND pr.opportunity_score >= ? "
        "AND substr(COALESCE(p.first_seen_at, p.last_updated_at, ''), 1, 10) = ?",
        (threshold, today))
    new_issued = _n(
        "SELECT COUNT(*) AS n FROM projects pr JOIN permits p ON p.id=pr.permit_id "
        "WHERE pr.project_lifecycle='Permit Issued' "
        "AND substr(COALESCE(p.issued_date, ''), 1, 10) = ?",
        (today,))
    # Relevance and today's actions come from the shared trust layer, not the
    # legacy company_priority tier.
    from pipeline.trust import account_view as trust
    rel_join, rel_rank, rel_params = trust.relevance_sql("c.id")
    relevant_accounts = _n(
        f"SELECT COUNT(*) AS n FROM companies c {rel_join} "
        f"WHERE c.lifecycle_state='active' AND {rel_rank} = {trust.RANK[trust.CORE]}",
        tuple(rel_params))
    dnc = {r["company_id"] for r in conn.execute(
        "SELECT company_id FROM crm_company_relationships WHERE organization_id=? "
        "AND (do_not_contact=1 OR relationship_status='do_not_contact')", (org_id,))}
    try:
        todays = trust.todays_accounts(conn, exclude_ids=dnc)
    except sqlite3.Error:
        todays = {"items": [], "data_status": {"refresh": "Unknown"},
                  "note": "Account checks are unavailable right now."}
    high_priority = len(todays["items"])
    awaiting_assignment = _n(
        "SELECT COUNT(*) AS n FROM companies c "
        "WHERE c.lifecycle_state='active' AND NOT EXISTS ("
        "  SELECT 1 FROM crm_company_relationships r "
        "  WHERE r.company_id=c.id AND r.organization_id=? AND r.assigned_user_id IS NOT NULL)",
        (org_id,))
    estimates_awaiting = _n(
        "SELECT COUNT(*) AS n FROM projects pr "
        "WHERE pr.project_lifecycle IN ('Permit Issued','Construction Active') "
        "AND pr.opportunity_score >= ? "
        "AND NOT EXISTS (SELECT 1 FROM estimated_materials em WHERE em.project_id=pr.id)",
        (threshold,))
    estimates_approved = _n(
        "SELECT COUNT(*) AS n FROM estimated_materials")
    active_employees = _n(
        "SELECT COUNT(*) AS n FROM users WHERE organization_id=? AND is_active=1", (org_id,))
    overdue_team_tasks = _n(
        "SELECT COUNT(*) AS n FROM crm_tasks WHERE organization_id=? "
        "AND status IN ('open','in_progress') AND due_at IS NOT NULL "
        "AND substr(due_at,1,10) < ?", (org_id, today))

    refresh = pipeline_runs.admin_status(conn)
    simple = pipeline_runs.employee_status(conn)
    summary = refresh.get("summary") or {}

    # Prefer today's live counts; fall back to the morning-refresh summary only
    # when that run completed today. An older summary's "new" counts are not
    # today's and are never shown as if they were.
    summary_is_today = str(summary.get("completed_at") or "")[:10] == today
    if summary_is_today and new_submitted == 0 and summary.get("new_submitted_opportunities") is not None:
        new_submitted = int(summary["new_submitted_opportunities"] or 0)
    if summary_is_today and new_issued == 0 and summary.get("new_issued_permits") is not None:
        new_issued = int(summary["new_issued_permits"] or 0)

    recent_pipeline = [
        {
            "id": r["id"], "status": r["status"], "started_at": r["started_at"],
            "completed_at": r["completed_at"], "trigger_source": r.get("trigger_source"),
            "jurisdictions_succeeded": r.get("jurisdictions_succeeded"),
            "jurisdictions_failed": r.get("jurisdictions_failed"),
            "records_created": r.get("records_created"),
            "records_updated": r.get("records_updated"),
        }
        for r in (refresh.get("history") or [])[:8]
    ]

    # Recent = a source activity date (issued, else filed, else stored
    # opportunity date) inside the shared window. Ingestion timestamps never
    # stand in for activity dates, so an undated record cannot look new.
    from pipeline.crm.service import _attach_recency, source_health
    from pipeline.trust import recency

    start, end = recency.window_bounds()
    recent_opps = [
        dict(r) for r in conn.execute(
            f"""
            SELECT pr.id AS project_id, c.id AS company_id, c.display_name,
                   p.jurisdiction, p.job_address, p.city, pr.project_lifecycle,
                   pr.opportunity_score, pr.opportunity_date, pr.opportunity_timing,
                   pr.project_category, p.permit_type, p.permit_subtype, p.description,
                   p.project_description, p.issued_date, p.filed_date
            FROM projects pr
            JOIN permits p ON p.id = pr.permit_id
            LEFT JOIN companies c ON c.id = COALESCE(pr.contractor_company_id, p.contractor_company_id)
            WHERE pr.opportunity_score IS NOT NULL
              AND {recency.ACTIVITY_DATE_SQL} >= ? AND {recency.ACTIVITY_DATE_SQL} <= ?
            ORDER BY {recency.ACTIVITY_DATE_SQL} DESC, pr.id DESC
            LIMIT ?
            """,
            (start, end, 200),
        ).fetchall()
    ]
    recent_opps = trust.annotate_projects(recent_opps, conn=conn)[:10]
    health = source_health(conn)
    _attach_recency(conn, recent_opps, health)

    freshness = refresh.get("freshness")
    if freshness is None:
        # No run summary (no refresh yet, one running, or one that crashed
        # before writing its summary). Read freshness from the data with the
        # trust-layer rule; a sync status alone never makes a feed "Current".
        freshness = _trust_freshness_rows(conn, health)
    _mark_outreach_blocked(freshness, health)

    return {
        "scope": "organization",
        "assignment_scoped": False,
        "kpis": {
            "total_companies": total_companies,
            "total_projects": total_projects,
            "total_permits": total_permits,
            "new_submitted_opportunities": new_submitted,
            "new_issued_permits": new_issued,
            "high_priority_opportunities": high_priority,
            "accounts_to_act_on_today": high_priority,
            "relevant_accounts": relevant_accounts,
            "companies_awaiting_assignment": awaiting_assignment,
            "estimates_awaiting_review": estimates_awaiting,
            "estimates_approved_for_supplier": estimates_approved,
            "active_employees": active_employees,
            "overdue_team_tasks": overdue_team_tasks,
        },
        "morning_refresh": {
            "label": simple.get("label"),
            "status": simple.get("status"),
            "last_completed": simple.get("last_completed"),
            "last_successful": simple.get("last_successful"),
            "freshness": simple.get("freshness"),
            "stale": simple.get("stale"),
            "running": refresh.get("running"),
            "summary": summary,
            "summary_is_today": summary_is_today,
        },
        "jurisdiction_freshness": freshness or [],
        "recent_pipeline_activity": recent_pipeline,
        "recent_opportunity_activity": recent_opps,
        "todays_accounts": todays,
    }


_TRUST_STATUS = {"FRESH": "Current", "LAGGING": "Delayed", "STALE": "Stale", "NO_DATA": "No data"}


def _trust_freshness_rows(conn, health: dict | None) -> list[dict]:
    """Jurisdiction freshness from the trust layer (newest source record date),
    for when no refresh summary exists. Unknown is never reported as Current."""
    names = {r["slug"]: r["name"] for r in conn.execute("SELECT slug, name FROM jurisdictions")}
    rows = [
        {"slug": s["jurisdiction"], "name": names.get(s["jurisdiction"], s["jurisdiction"]),
         "status": _TRUST_STATUS.get(s.get("freshness"), "Unknown"),
         "newest_source_date": s.get("newest_record_date"),
         "days_since_newest": s.get("days_since_newest_record"),
         "records_received_today": None, "basis": "trust_layer"}
        for s in (health or {}).get("sources") or []
    ]
    return sorted(rows, key=lambda r: (r["status"] == "Current", r["name"] or ""))


def _mark_outreach_blocked(rows: list[dict], health: dict | None) -> None:
    """Flag jurisdictions whose feed the trust layer blocks from Today's
    accounts, so the admin panel and the queue agree on what is current."""
    sources = {s["jurisdiction"]: s for s in (health or {}).get("sources") or []}
    for row in rows or []:
        src = sources.get(row.get("slug"))
        row["outreach_blocked"] = True if src is None else bool(src.get("call_blocked"))
        row["trust_freshness"] = None if src is None else src.get("freshness")


def estimator_work_queue(conn: sqlite3.Connection, user: dict) -> dict:
    """Assigned / high-score projects ready for estimate work."""
    from pipeline.auth.rbac import has_permission
    from pipeline.config import settings

    if not (has_permission(user, "projects.view_assigned")
            or has_permission(user, "projects.view")
            or has_permission(user, "admin.system")):
        raise AuthzError("missing permission: projects.view_assigned")

    threshold = getattr(settings, "REPORT_MIN_OPPORTUNITY_SCORE", 60)
    rows = conn.execute(
        """
        SELECT pr.id AS project_id, pr.opportunity_score, pr.project_lifecycle,
               pr.estimated_material_value, p.permit_number, p.job_address, p.city,
               p.status AS permit_status, c.id AS company_id, c.display_name AS company_name,
               p.permit_type, p.permit_subtype, p.description, p.project_description
        FROM projects pr
        JOIN permits p ON p.id = pr.permit_id
        LEFT JOIN companies c ON c.id = p.contractor_company_id
        WHERE pr.project_lifecycle IN ('Permit Issued','Construction Active')
          AND pr.opportunity_score >= ?
          AND NOT EXISTS (SELECT 1 FROM estimated_materials em WHERE em.project_id=pr.id)
        ORDER BY pr.opportunity_score DESC
        LIMIT 1000
        """,
        (threshold,),
    ).fetchall()
    # A material estimate needs wet-side scope and a contractor in the
    # plumbing-supply focus; permits naming only other trades (racking,
    # electrical, signage) or held by off-focus accounts are not estimator work.
    from pipeline.trust.account_view import annotate_projects
    items = annotate_projects([dict(r) for r in rows], conn=conn)[:50]
    submitted = [
        dict(r) for r in conn.execute(
            """
            SELECT pr.id AS project_id, pr.opportunity_score, pr.project_lifecycle,
                   p.permit_number, p.job_address, p.city, c.display_name AS company_name
            FROM projects pr
            JOIN permits p ON p.id = pr.permit_id
            LEFT JOIN companies c ON c.id = p.contractor_company_id
            WHERE EXISTS (SELECT 1 FROM estimated_materials em WHERE em.project_id=pr.id)
            ORDER BY pr.id DESC LIMIT 25
            """
        ).fetchall()
    ]
    return {
        "scope": "estimator",
        "assignment_scoped": True,
        "queue": items,
        "my_estimates": items[:25],
        "submitted_estimates": submitted,
        "kpis": {
            "queue_count": len(items),
            "submitted_count": len(submitted),
        },
    }
