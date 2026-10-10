"""Manager/admin operations: team overview and user management. All checks
enforce permissions and organization boundaries. Password hashes and security
counters are never serialized out."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline.auth.rbac import AuthzError, ROLES, require_permission
from pipeline.auth.service import create_user as _create_user, write_audit
from pipeline.auth.onboarding import approve_supplier, suspend_account
from pipeline.crm import serializers
from pipeline.crm.service import ValidationError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _roles_for(conn, user_id):
    return [r["name"] for r in conn.execute(
        "SELECT r.name FROM user_roles ur JOIN roles r ON r.id=ur.role_id WHERE ur.user_id=?",
        (user_id,))]


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
        kind = d.get("account_kind") or "employee"
        d["email_verified"] = bool(d.get("email_verified_at")) or kind == "employee"
        out.append(d)
    return out


def create_user(conn: sqlite3.Connection, user: dict, data: dict, *, ip=None, ua=None) -> dict:
    require_permission(user, "users.create")
    email = (data.get("email") or "").strip()
    roles = data.get("roles") or ["sales_representative"]
    for rn in roles:
        if rn not in ROLES:
            raise ValidationError(f"unknown role: {rn}")
        if rn in ("contractor", "supplier"):
            raise ValidationError("Contractor and supplier accounts are created through self-registration.")
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
    updates = {}
    for k in ("first_name", "last_name", "display_name", "phone"):
        if k in data:
            require_permission(user, "users.update")
            updates[k] = data[k]
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
        conn.execute("DELETE FROM user_roles WHERE user_id=?", (target_id,))
        for rn in data["roles"]:
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


def approve_supplier_account(conn, user, target_id, *, ip=None, ua=None) -> dict:
    ctx = approve_supplier(conn, user, target_id, ip=ip, ua=ua)
    row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    out = serializers.serialize_user(row)
    out["roles"] = _roles_for(conn, target_id)
    out["email_verified"] = ctx.get("email_verified")
    return out


def suspend_user_account(conn, user, target_id, *, ip=None, ua=None) -> dict:
    ctx = suspend_account(conn, user, target_id, ip=ip, ua=ua)
    row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
    out = serializers.serialize_user(row)
    out["roles"] = _roles_for(conn, target_id)
    out["account_state"] = ctx.get("account_state")
    return out


def team_overview(conn: sqlite3.Connection, user: dict) -> list[dict]:
    require_permission(user, "users.view")
    org_id = user["organization_id"]
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    kind_filter = " AND (account_kind IS NULL OR account_kind='employee')" if "account_kind" in cols else ""
    rows = conn.execute(
        "SELECT id, display_name, email FROM users WHERE organization_id=? AND is_active=1"
        f"{kind_filter} ORDER BY display_name", (org_id,)).fetchall()

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
        "AND substr(COALESCE(p.issued_date, p.last_updated_at, ''), 1, 10) = ?",
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

    # Prefer today's live counts; fall back to latest morning-refresh summary.
    if new_submitted == 0 and summary.get("new_submitted_opportunities") is not None:
        new_submitted = int(summary["new_submitted_opportunities"] or 0)
    if new_issued == 0 and summary.get("new_issued_permits") is not None:
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

    recent_opps = [
        dict(r) for r in conn.execute(
            """
            SELECT pr.id AS project_id, c.id AS company_id, c.display_name,
                   p.jurisdiction, p.job_address, p.city, pr.project_lifecycle,
                   pr.opportunity_score, pr.opportunity_date, pr.opportunity_timing,
                   pr.project_category, p.permit_type, p.permit_subtype, p.description,
                   p.project_description
            FROM projects pr
            JOIN permits p ON p.id = pr.permit_id
            LEFT JOIN companies c ON c.id = COALESCE(pr.contractor_company_id, p.contractor_company_id)
            WHERE pr.opportunity_score IS NOT NULL
            ORDER BY COALESCE(pr.opportunity_date, p.last_updated_at) DESC
            LIMIT ?
            """,
            (200,),
        ).fetchall()
    ]
    recent_opps = trust.annotate_projects(recent_opps, conn=conn)[:10]

    freshness = refresh.get("freshness")
    if freshness is None:
        # Build a simple jurisdiction list when no run summary exists yet.
        freshness = [
            {"slug": r["slug"], "name": r["name"],
             "status": "Current" if r["last_sync_status"] in ("success", "success_with_errors", None)
             else (r["last_sync_status"] or "Unknown"),
             "newest_source_date": r["last_synced_at"],
             "records_received_today": 0}
            for r in conn.execute(
                "SELECT slug, name, last_synced_at, last_sync_status FROM jurisdictions "
                "ORDER BY name").fetchall()
        ]

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
            "running": refresh.get("running"),
            "summary": summary,
        },
        "jurisdiction_freshness": freshness or [],
        "recent_pipeline_activity": recent_pipeline,
        "recent_opportunity_activity": recent_opps,
        "todays_accounts": todays,
    }


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
