"""CRM service layer. Every function enforces authentication context (a user
dict from the auth layer), permissions, organization boundaries, and
record-level company access *before* touching data. Intelligence tables are
never modified here."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline.auth.rbac import AuthzError, can_access_company, has_permission, require_permission
from pipeline.auth.service import write_audit
from pipeline.company_resolution import queries as ci_queries
from pipeline.crm import scan_cache, serializers
from pipeline.trust import account_view as trust
from pipeline.trust.contacts import verified_contact_view
from pipeline.trust.public import public_relevance

RELATIONSHIP_STATUSES = {
    "new", "assigned", "researching", "attempted_contact", "contacted",
    "qualified", "follow_up", "quote_requested", "quote_sent", "negotiating",
    "won", "lost", "do_not_contact", "inactive",
}
ACTIVITY_TYPES = {
    "call", "voicemail", "email", "text_message", "meeting", "site_visit",
    "note", "quote_request", "quote_sent", "follow_up", "status_change",
    "assignment",
}
ACTIVITY_OUTCOMES = {
    "no_answer", "left_voicemail", "spoke_with_contact", "interested",
    "not_interested", "follow_up_requested", "appointment_set",
    "quote_requested", "wrong_number", "invalid_contact", "do_not_contact",
    "completed", "other",
}
TASK_STATUSES = {"open", "in_progress", "completed", "cancelled", "overdue"}
TASK_PRIORITIES = {"low", "normal", "high", "urgent"}

_STATUS_TIMESTAMP = {
    "qualified": "qualified_at",
    "won": "won_at",
    "lost": "lost_at",
}


class ValidationError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.status = 400


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_access(conn: sqlite3.Connection, user: dict, company_id: int) -> None:
    if not can_access_company(conn, user, company_id):
        raise AuthzError("company not assigned to you")


def _company_exists(conn: sqlite3.Connection, company_id: int) -> bool:
    return conn.execute("SELECT 1 FROM companies WHERE id=?", (company_id,)).fetchone() is not None


# --------------------------------------------------------------------------
# Read-only intelligence (serialized; sales never gets restricted fields)
# --------------------------------------------------------------------------

def get_company_detail(conn: sqlite3.Connection, user: dict, company_id: int,
                       *, ip=None, ua=None) -> dict:
    _require_access(conn, user, company_id)
    base = ci_queries.get_company(conn, company_id)
    if base is None:
        raise ValidationError("unknown company")
    rel = _get_relationship_row(conn, user["organization_id"], company_id)
    write_audit(conn, event_type="company_viewed", success=True, user_id=user["id"],
                organization_id=user["organization_id"], resource_type="company",
                resource_id=company_id, ip_address=ip, user_agent=ua)
    return {
        "company": serializers.serialize_company(base["company"]),
        "intelligence": serializers.serialize_intelligence(base["intelligence"] or {}),
        "roles": base["roles"],
        "aliases": base["aliases"],
        "contacts": base["contacts"],
        # Best verified contact for this exact company, from the shared trust layer.
        "verified_contact": verified_contact_view(conn, company_id),
        "data_quality": base["data_quality"],
        "relationship": serializers.serialize_relationship(rel) if rel else None,
    }


def get_company_projects(conn, user, company_id):
    _require_access(conn, user, company_id)
    require_permission(user, "projects.view_assigned")
    return ci_queries.company_projects(conn, company_id)


def get_company_permits(conn, user, company_id):
    _require_access(conn, user, company_id)
    require_permission(user, "permits.view")
    return ci_queries.company_permits(conn, company_id)


def get_company_timeline(conn, user, company_id):
    _require_access(conn, user, company_id)
    return ci_queries.company_timeline(conn, company_id)


# --------------------------------------------------------------------------
# Relationships
# --------------------------------------------------------------------------

def _get_relationship_row(conn, org_id, company_id):
    return conn.execute(
        "SELECT * FROM crm_company_relationships WHERE organization_id=? AND company_id=?",
        (org_id, company_id),
    ).fetchone()


def ensure_relationship(conn: sqlite3.Connection, org_id: int, company_id: int) -> int:
    """Get or create the single relationship row for org/company."""
    row = _get_relationship_row(conn, org_id, company_id)
    if row is not None:
        return row["id"]
    now = _now()
    cur = conn.execute(
        "INSERT INTO crm_company_relationships (organization_id, company_id, "
        "relationship_status, created_at, updated_at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(organization_id, company_id) DO NOTHING",
        (org_id, company_id, "new", now, now),
    )
    conn.commit()
    if cur.lastrowid:
        return cur.lastrowid
    return _get_relationship_row(conn, org_id, company_id)["id"]


def get_relationship(conn: sqlite3.Connection, user: dict, company_id: int) -> dict | None:
    require_permission(user, "crm.relationships.view")
    _require_access(conn, user, company_id)
    row = _get_relationship_row(conn, user["organization_id"], company_id)
    return serializers.serialize_relationship(row) if row else None


def update_relationship(conn: sqlite3.Connection, user: dict, company_id: int,
                        data: dict, *, ip=None, ua=None) -> dict:
    require_permission(user, "crm.relationships.update")
    if not _company_exists(conn, company_id):
        raise ValidationError("unknown company")
    _require_access(conn, user, company_id)
    org_id = user["organization_id"]
    rel_id = ensure_relationship(conn, org_id, company_id)
    current = _get_relationship_row(conn, org_id, company_id)

    updates: dict = {}
    editable = ("lead_source", "priority_override", "do_not_contact_reason",
                "next_followup_at", "lost_reason")
    for k in editable:
        if k in data:
            updates[k] = data[k]
    if "do_not_contact" in data:
        updates["do_not_contact"] = 1 if data["do_not_contact"] else 0

    status_changed = False
    new_status = data.get("relationship_status")
    if new_status is not None:
        if new_status not in RELATIONSHIP_STATUSES:
            raise ValidationError(f"invalid relationship_status: {new_status}")
        if new_status != current["relationship_status"]:
            status_changed = True
            updates["relationship_status"] = new_status
            ts_col = _STATUS_TIMESTAMP.get(new_status)
            if ts_col:
                updates[ts_col] = _now()

    if not updates:
        return serializers.serialize_relationship(_get_relationship_row(conn, org_id, company_id))

    updates["updated_at"] = _now()
    set_sql = ", ".join(f"{k}=?" for k in updates)
    conn.execute(
        f"UPDATE crm_company_relationships SET {set_sql} WHERE id=?",
        [*updates.values(), rel_id],
    )
    conn.commit()

    if status_changed:
        # Status changes create separate timeline events.
        _insert_activity(conn, org_id=org_id, company_id=company_id,
                         user_id=user["id"], activity_type="status_change",
                         subject=f"Status -> {new_status}",
                         notes=data.get("status_note"))
        write_audit(conn, event_type="relationship_status_changed", success=True,
                    user_id=user["id"], organization_id=org_id,
                    resource_type="company", resource_id=company_id,
                    action=new_status, ip_address=ip, user_agent=ua)
    return serializers.serialize_relationship(_get_relationship_row(conn, org_id, company_id))


# --------------------------------------------------------------------------
# Activities
# --------------------------------------------------------------------------

def _insert_activity(conn, *, org_id, company_id, user_id, activity_type,
                     activity_outcome=None, subject=None, notes=None,
                     project_id=None, permit_id=None, contact_id=None,
                     activity_at=None, duration_minutes=None, next_followup_at=None) -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO crm_activities (organization_id, company_id, project_id, "
        "permit_id, contact_id, user_id, activity_type, activity_outcome, subject, "
        "notes, activity_at, duration_minutes, next_followup_at, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (org_id, company_id, project_id, permit_id, contact_id, user_id,
         activity_type, activity_outcome, subject, notes, activity_at or now,
         duration_minutes, next_followup_at, now, now),
    )
    conn.commit()
    return cur.lastrowid


def list_activities(conn: sqlite3.Connection, user: dict, company_id: int,
                    limit: int = 200) -> list[dict]:
    require_permission(user, "crm.activities.view")
    _require_access(conn, user, company_id)
    rows = conn.execute(
        "SELECT * FROM crm_activities WHERE organization_id=? AND company_id=? "
        "ORDER BY activity_at DESC, id DESC LIMIT ?",
        (user["organization_id"], company_id, limit),
    ).fetchall()
    return [serializers.serialize_activity(r) for r in rows]


def create_activity(conn: sqlite3.Connection, user: dict, company_id: int,
                    data: dict, *, ip=None, ua=None) -> dict:
    require_permission(user, "crm.activities.create")
    if not _company_exists(conn, company_id):
        raise ValidationError("unknown company")
    _require_access(conn, user, company_id)
    activity_type = data.get("activity_type")
    if activity_type not in ACTIVITY_TYPES:
        raise ValidationError(f"invalid activity_type: {activity_type}")
    outcome = data.get("activity_outcome")
    if outcome is not None and outcome not in ACTIVITY_OUTCOMES:
        raise ValidationError(f"invalid activity_outcome: {outcome}")
    org_id = user["organization_id"]
    now = _now()
    next_followup = data.get("next_followup_at")
    activity_id = _insert_activity(
        conn, org_id=org_id, company_id=company_id, user_id=user["id"],
        activity_type=activity_type, activity_outcome=outcome,
        subject=data.get("subject"), notes=data.get("notes"),
        project_id=data.get("project_id"), permit_id=data.get("permit_id"),
        contact_id=data.get("contact_id"), activity_at=data.get("activity_at"),
        duration_minutes=data.get("duration_minutes"), next_followup_at=next_followup,
    )
    # Reflect contact recency + follow-up on the relationship (no intel writes).
    rel_id = ensure_relationship(conn, org_id, company_id)
    rel = _get_relationship_row(conn, org_id, company_id)
    rel_updates = {"last_contact_at": now, "updated_at": now}
    if rel["first_contact_at"] is None:
        rel_updates["first_contact_at"] = now
    if next_followup:
        rel_updates["next_followup_at"] = next_followup
    set_sql = ", ".join(f"{k}=?" for k in rel_updates)
    conn.execute(f"UPDATE crm_company_relationships SET {set_sql} WHERE id=?",
                 [*rel_updates.values(), rel_id])
    conn.commit()
    write_audit(conn, event_type="activity_created", success=True, user_id=user["id"],
                organization_id=org_id, resource_type="crm_activity",
                resource_id=activity_id, action=activity_type, ip_address=ip, user_agent=ua)
    row = conn.execute("SELECT * FROM crm_activities WHERE id=?", (activity_id,)).fetchone()
    return serializers.serialize_activity(row)


def update_activity(conn: sqlite3.Connection, user: dict, activity_id: int,
                    data: dict, *, ip=None, ua=None) -> dict:
    row = conn.execute("SELECT * FROM crm_activities WHERE id=?", (activity_id,)).fetchone()
    if row is None or row["organization_id"] != user["organization_id"]:
        raise AuthzError("activity not found", status=404)
    if row["user_id"] == user["id"]:
        require_permission(user, "crm.activities.edit_own")
    else:
        require_permission(user, "crm.activities.edit_all")
    # Retain revision history.
    import json
    prev = {k: row[k] for k in ("activity_type", "activity_outcome", "subject",
                                "notes", "next_followup_at")}
    conn.execute(
        "INSERT INTO crm_activity_revisions (activity_id, edited_by, "
        "previous_values_json, created_at) VALUES (?,?,?,?)",
        (activity_id, user["id"], json.dumps(prev), _now()),
    )
    editable = {}
    for k in ("activity_outcome", "subject", "notes", "next_followup_at", "duration_minutes"):
        if k in data:
            editable[k] = data[k]
    if "activity_outcome" in editable and editable["activity_outcome"] not in (None, *ACTIVITY_OUTCOMES):
        raise ValidationError("invalid activity_outcome")
    if editable:
        editable["updated_at"] = _now()
        set_sql = ", ".join(f"{k}=?" for k in editable)
        conn.execute(f"UPDATE crm_activities SET {set_sql} WHERE id=?",
                     [*editable.values(), activity_id])
        conn.commit()
    write_audit(conn, event_type="activity_updated", success=True, user_id=user["id"],
                organization_id=user["organization_id"], resource_type="crm_activity",
                resource_id=activity_id, ip_address=ip, user_agent=ua)
    row = conn.execute("SELECT * FROM crm_activities WHERE id=?", (activity_id,)).fetchone()
    return serializers.serialize_activity(row)


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------

def list_tasks(conn: sqlite3.Connection, user: dict, filters: dict | None = None) -> list[dict]:
    require_permission(user, "crm.tasks.view")
    filters = filters or {}
    where = ["organization_id=?"]
    params: list = [user["organization_id"]]
    # Reps/read-only see their own tasks; task assigners (managers) see all org tasks.
    if not has_permission(user, "crm.tasks.assign"):
        where.append("(assigned_user_id=? OR created_by_user_id=?)")
        params += [user["id"], user["id"]]
    status = (filters.get("status") or "").strip()
    if status:
        where.append("status=?")
        params.append(status)
    rows = conn.execute(
        f"SELECT t.*, c.display_name AS company_name, u.display_name AS assigned_to "
        f"FROM crm_tasks t LEFT JOIN companies c ON c.id=t.company_id "
        f"LEFT JOIN users u ON u.id=t.assigned_user_id "
        f"WHERE {' AND '.join(where).replace('organization_id', 't.organization_id')} "
        f"ORDER BY COALESCE(t.due_at,'9999') ASC, t.id DESC LIMIT 500",
        params,
    ).fetchall()
    out = []
    for r in rows:
        d = serializers.serialize_task(r)
        d["company_name"] = r["company_name"]
        d["assigned_to"] = r["assigned_to"]
        out.append(d)
    return out


def create_task(conn: sqlite3.Connection, user: dict, data: dict, *, ip=None, ua=None) -> dict:
    require_permission(user, "crm.tasks.create")
    title = (data.get("title") or "").strip()
    if not title:
        raise ValidationError("title is required")
    priority = data.get("priority") or "normal"
    if priority not in TASK_PRIORITIES:
        raise ValidationError(f"invalid priority: {priority}")
    assigned_user_id = data.get("assigned_user_id") or user["id"]
    if assigned_user_id != user["id"]:
        require_permission(user, "crm.tasks.assign")
        _assert_same_org_user(conn, user, assigned_user_id)
    company_id = data.get("company_id")
    if company_id:
        _require_access(conn, user, company_id)
    now = _now()
    cur = conn.execute(
        "INSERT INTO crm_tasks (organization_id, company_id, project_id, "
        "assigned_user_id, created_by_user_id, task_type, title, description, "
        "priority, due_at, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["organization_id"], company_id, data.get("project_id"),
         assigned_user_id, user["id"], data.get("task_type"), title,
         data.get("description"), priority, data.get("due_at"), "open", now, now),
    )
    conn.commit()
    task_id = cur.lastrowid
    write_audit(conn, event_type="task_created", success=True, user_id=user["id"],
                organization_id=user["organization_id"], resource_type="crm_task",
                resource_id=task_id, ip_address=ip, user_agent=ua)
    row = conn.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,)).fetchone()
    return serializers.serialize_task(row)


def update_task(conn: sqlite3.Connection, user: dict, task_id: int, data: dict,
                *, ip=None, ua=None) -> dict:
    row = conn.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,)).fetchone()
    if row is None or row["organization_id"] != user["organization_id"]:
        raise AuthzError("task not found", status=404)
    is_owner = user["id"] in (row["assigned_user_id"], row["created_by_user_id"])

    updates: dict = {}
    if "assigned_user_id" in data and data["assigned_user_id"] != row["assigned_user_id"]:
        require_permission(user, "crm.tasks.assign")
        _assert_same_org_user(conn, user, data["assigned_user_id"])
        updates["assigned_user_id"] = data["assigned_user_id"]

    completing = data.get("status") == "completed" and row["status"] != "completed"
    if "status" in data:
        if data["status"] not in TASK_STATUSES:
            raise ValidationError(f"invalid status: {data['status']}")
        if completing:
            require_permission(user, "crm.tasks.complete")
            updates["completed_at"] = _now()
        elif not is_owner:
            require_permission(user, "crm.tasks.assign")
        else:
            require_permission(user, "crm.tasks.edit_own")
        updates["status"] = data["status"]

    for k in ("title", "description", "priority", "due_at", "task_type"):
        if k in data:
            if not is_owner and "assigned_user_id" not in updates:
                require_permission(user, "crm.tasks.assign")
            if k == "priority" and data[k] not in TASK_PRIORITIES:
                raise ValidationError(f"invalid priority: {data[k]}")
            updates[k] = data[k]

    if updates:
        updates["updated_at"] = _now()
        set_sql = ", ".join(f"{k}=?" for k in updates)
        conn.execute(f"UPDATE crm_tasks SET {set_sql} WHERE id=?",
                     [*updates.values(), task_id])
        conn.commit()
    if completing:
        write_audit(conn, event_type="task_completed", success=True, user_id=user["id"],
                    organization_id=user["organization_id"], resource_type="crm_task",
                    resource_id=task_id, ip_address=ip, user_agent=ua)
    row = conn.execute("SELECT * FROM crm_tasks WHERE id=?", (task_id,)).fetchone()
    return serializers.serialize_task(row)


# --------------------------------------------------------------------------
# Assignments
# --------------------------------------------------------------------------

def _assert_same_org_user(conn: sqlite3.Connection, user: dict, target_user_id: int) -> None:
    row = conn.execute("SELECT organization_id FROM users WHERE id=?", (target_user_id,)).fetchone()
    if row is None or row["organization_id"] != user["organization_id"]:
        raise ValidationError("target user not in your organization")


def assign_company(conn: sqlite3.Connection, user: dict, company_id: int,
                   new_user_id: int, *, reason: str | None = None, ip=None, ua=None) -> dict:
    require_permission(user, "companies.assign")
    if not _company_exists(conn, company_id):
        raise ValidationError("unknown company")
    _assert_same_org_user(conn, user, new_user_id)
    org_id = user["organization_id"]
    rel_id = ensure_relationship(conn, org_id, company_id)
    current = _get_relationship_row(conn, org_id, company_id)
    previous_user_id = current["assigned_user_id"]
    now = _now()
    new_status = current["relationship_status"]
    if new_status in ("new", None):
        new_status = "assigned"
    conn.execute(
        "UPDATE crm_company_relationships SET assigned_user_id=?, assigned_by=?, "
        "assigned_at=?, relationship_status=?, updated_at=? WHERE id=?",
        (new_user_id, user["id"], now, new_status, now, rel_id),
    )
    conn.execute(
        "INSERT INTO crm_assignment_history (organization_id, company_id, "
        "previous_user_id, new_user_id, assigned_by, reason, assigned_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (org_id, company_id, previous_user_id, new_user_id, user["id"], reason, now),
    )
    conn.commit()
    write_audit(conn, event_type="assignment_changed", success=True, user_id=user["id"],
                organization_id=org_id, resource_type="company", resource_id=company_id,
                action="assign", details={"previous_user_id": previous_user_id,
                                          "new_user_id": new_user_id, "reason": reason},
                ip_address=ip, user_agent=ua)
    return serializers.serialize_relationship(_get_relationship_row(conn, org_id, company_id))


def list_assignments(conn: sqlite3.Connection, user: dict) -> list[dict]:
    # Managers/admins with assign or users.view can review assignments.
    if not (has_permission(user, "companies.assign") or has_permission(user, "users.view")):
        raise AuthzError("missing permission: companies.assign")
    rows = conn.execute(
        """
        SELECT r.company_id, c.display_name AS company_name, r.relationship_status,
               r.assigned_user_id, u.display_name AS assigned_to,
               r.assigned_at, r.next_followup_at, r.last_contact_at
        FROM crm_company_relationships r
        JOIN companies c ON c.id = r.company_id
        LEFT JOIN users u ON u.id = r.assigned_user_id
        WHERE r.organization_id=?
        ORDER BY r.updated_at DESC LIMIT 500
        """,
        (user["organization_id"],),
    ).fetchall()
    return [dict(r) for r in rows]


def assignment_history(conn: sqlite3.Connection, user: dict, company_id: int) -> list[dict]:
    if not (has_permission(user, "companies.assign") or can_access_company(conn, user, company_id)):
        raise AuthzError("permission denied")
    rows = conn.execute(
        "SELECT * FROM crm_assignment_history WHERE organization_id=? AND company_id=? "
        "ORDER BY assigned_at DESC, id DESC",
        (user["organization_id"], company_id),
    ).fetchall()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------
# My companies + dashboard
# --------------------------------------------------------------------------

def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def recommended_action(rel_status, next_followup_at, last_contact_at,
                       do_not_contact=0) -> str:
    """Server-side next-action guidance (no business logic left to the browser)."""
    today = _today()
    if do_not_contact or rel_status == "do_not_contact":
        return "Do not contact"
    if rel_status == "won":
        return "Maintain relationship"
    if rel_status == "lost":
        return "Closed — lost"
    if next_followup_at:
        day = str(next_followup_at)[:10]
        if day <= today:
            return "Follow up now (due)"
        return f"Follow up on {day}"
    if not last_contact_at:
        return "Make first contact"
    if rel_status in ("quote_requested",):
        return "Send quote"
    if rel_status in ("quote_sent", "negotiating"):
        return "Advance the deal"
    if rel_status in ("qualified", "contacted", "follow_up", "researching",
                      "attempted_contact"):
        return "Continue outreach"
    return "Review and reach out"


def priority_reason(row) -> str:
    """Short human explanation of why a company is prioritized."""
    tier = row["company_priority_tier"] if _has(row, "company_priority_tier") else None
    score = row["company_priority_score"] if _has(row, "company_priority_score") else None
    bits = []
    if tier:
        bits.append(f"{tier} priority")
    if score is not None:
        bits.append(f"score {round(score)}")
    recent = row["projects_last_30_days"] if _has(row, "projects_last_30_days") else None
    if recent:
        bits.append(f"{recent} new project{'s' if recent != 1 else ''} in 30d")
    elif _has(row, "active_projects") and row["active_projects"]:
        bits.append(f"{row['active_projects']} active project{'s' if row['active_projects'] != 1 else ''}")
    return " · ".join(bits) if bits else "Assigned opportunity"


_RELEVANCE_FILTERS = {
    "promotable": trust.PROMOTABLE,
    "core": (trust.CORE,),
    "adjacent": (trust.ADJACENT,),
    "relevant": (trust.CORE, trust.ADJACENT),
    "not_relevant": (trust.NOT_RELEVANT, trust.NOT_SALES_READY),
}


def relevance_reason(rel: dict | None, row) -> str:
    """Why this account is (or is not) worth attention, in plain words.
    Built from account relevance and observed activity, not the legacy tier."""
    rel = rel or {"label": "Not yet assessed", "lanes": []}
    bits = [rel["label"] + (f" ({', '.join(rel['lanes'])})" if rel.get("lanes") else "")]
    recent = row["projects_last_30_days"] if _has(row, "projects_last_30_days") else None
    if recent:
        bits.append(f"{recent} new project{'s' if recent != 1 else ''} in 30d")
    elif _has(row, "active_projects") and row["active_projects"]:
        bits.append(f"{row['active_projects']} active project{'s' if row['active_projects'] != 1 else ''}")
    return " · ".join(bits)


def _has(row, key) -> bool:
    try:
        return key in row.keys()
    except AttributeError:
        return key in row


def _followup_overdue(next_followup_at) -> bool:
    return bool(next_followup_at) and str(next_followup_at)[:10] < _today()


def list_my_companies(conn: sqlite3.Connection, user: dict, filters: dict | None = None) -> dict:
    require_permission(user, "crm.relationships.view")
    from pipeline.config import settings as _s
    filters = filters or {}
    org_id = user["organization_id"]
    where = ["r.organization_id=?"]
    params: list = [org_id]
    # Record-level: reps see only their assignments; managers/admins see all org.
    if not has_permission(user, "companies.view"):
        where.append("r.assigned_user_id=?")
        params.append(user["id"])

    q = (filters.get("q") or "").strip()
    if q:
        where.append("(c.display_name LIKE ? OR c.city LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    status = (filters.get("status") or "").strip()
    if status:
        where.append("r.relationship_status=?")
        params.append(status)
    tier = (filters.get("tier") or "").strip()
    if tier:
        where.append("ci.company_priority_tier=?")
        params.append(tier)
    muni = (filters.get("municipality") or "").strip()
    if muni:
        where.append("EXISTS (SELECT 1 FROM projects pr WHERE pr.contractor_company_id=c.id AND pr.jurisdiction=?)")
        params.append(muni)
    lifecycle = (filters.get("lifecycle") or "").strip()
    if lifecycle:
        where.append("EXISTS (SELECT 1 FROM projects pr WHERE pr.contractor_company_id=c.id AND pr.project_lifecycle=?)")
        params.append(lifecycle)
    smin = filters.get("score_min")
    if smin not in (None, ""):
        where.append("COALESCE(ci.company_priority_score,0) >= ?")
        params.append(float(smin))
    smax = filters.get("score_max")
    if smax not in (None, ""):
        where.append("COALESCE(ci.company_priority_score,0) <= ?")
        params.append(float(smax))
    followup = (filters.get("followup") or "").strip()
    if followup == "due":
        where.append("r.next_followup_at IS NOT NULL AND substr(r.next_followup_at,1,10) <= ?")
        params.append(_today())
    elif followup == "overdue":
        where.append("r.next_followup_at IS NOT NULL AND substr(r.next_followup_at,1,10) < ?")
        params.append(_today())
    contacted = (filters.get("contacted") or "").strip()
    if contacted == "never":
        where.append("r.last_contact_at IS NULL")
    elif contacted == "yes":
        where.append("r.last_contact_at IS NOT NULL")
    if filters.get("do_not_contact") == "1":
        where.append("r.do_not_contact=1")
    rep = filters.get("assigned_user_id")
    if rep not in (None, "") and has_permission(user, "companies.view"):
        where.append("r.assigned_user_id=?")
        params.append(int(rep))
    # Account relevance comes from the shared trust layer (sales-lane assignment).
    rel_join, rel_rank, rel_params = trust.relevance_sql("c.id")
    relevance = (filters.get("relevance") or "").strip()
    wanted = _RELEVANCE_FILTERS.get(relevance)
    if wanted:
        where.append(f"{rel_rank} IN ({','.join(str(trust.RANK[s]) for s in wanted)})")

    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM crm_company_relationships r "
        f"JOIN companies c ON c.id=r.company_id "
        f"LEFT JOIN company_intelligence ci ON ci.company_id=c.id {rel_join} WHERE {where_sql}",
        [*rel_params, *params]).fetchone()["n"]

    page = max(1, int(filters.get("page", 1) or 1))
    try:
        page_size = int(filters.get("page_size") or _s.CRM_PAGE_SIZE_DEFAULT)
    except (TypeError, ValueError):
        page_size = _s.CRM_PAGE_SIZE_DEFAULT
    page_size = max(1, min(page_size, _s.CRM_PAGE_SIZE_MAX))

    rows = conn.execute(
        f"""
        SELECT c.id AS company_id, c.display_name, c.city, c.state,
               ci.company_priority_score, ci.company_priority_tier,
               ci.active_projects, ci.projects_last_30_days, ci.total_projects,
               ci.average_opportunity_score, ci.highest_opportunity_score,
               ci.latest_activity_date, ci.municipality_count,
               r.relationship_status, r.assigned_user_id, r.last_contact_at,
               r.next_followup_at, r.do_not_contact, u.display_name AS assigned_to,
               (SELECT role_type FROM company_roles cr WHERE cr.company_id=c.id
                ORDER BY is_primary DESC LIMIT 1) AS primary_role
        FROM crm_company_relationships r
        JOIN companies c ON c.id = r.company_id
        LEFT JOIN company_intelligence ci ON ci.company_id = c.id
        LEFT JOIN users u ON u.id = r.assigned_user_id
        {rel_join}
        WHERE {where_sql}
        ORDER BY {rel_rank} ASC, {trust.within_account_order()},
                 COALESCE(ci.company_priority_score,0) DESC,
                 ci.latest_activity_date DESC
        LIMIT ? OFFSET ?
        """,
        [*rel_params, *params, page_size, (page - 1) * page_size],
    ).fetchall()
    relevance_by_id = trust.relevance_for(conn, [r["company_id"] for r in rows])
    items = []
    for r in rows:
        d = dict(r)
        rel = relevance_by_id.get(r["company_id"])
        d["account_relevance"] = public_relevance(rel)
        d["reason"] = relevance_reason(rel, r)
        d["recommended_action"] = recommended_action(
            r["relationship_status"], r["next_followup_at"], r["last_contact_at"],
            r["do_not_contact"])
        off_focus = rel and rel["status"] in (trust.NOT_RELEVANT, trust.NOT_SALES_READY)
        if off_focus and d["recommended_action"] not in ("Do not contact", "Closed — lost"):
            d["recommended_action"] = "Review fit before more outreach"
        d["followup_overdue"] = _followup_overdue(r["next_followup_at"])
        items.append(d)
    return {"items": items, "total": total, "page": page, "page_size": page_size,
            "pages": (total + page_size - 1) // page_size if page_size else 1}


def dashboard(conn: sqlite3.Connection, user: dict) -> dict:
    org_id = user["organization_id"]
    uid = user["id"]
    today = _today()

    def _scalar(sql, params):
        return conn.execute(sql, params).fetchone()["n"]

    scoped = has_permission(user, "companies.view")
    assigned_filter = "" if scoped else "AND r.assigned_user_id=?"
    base_params = [org_id] if scoped else [org_id, uid]

    my_companies = _scalar(
        f"SELECT COUNT(*) AS n FROM crm_company_relationships r WHERE r.organization_id=? {assigned_filter}",
        base_params)
    overdue_followups = _scalar(
        f"SELECT COUNT(*) AS n FROM crm_company_relationships r WHERE r.organization_id=? "
        f"{assigned_filter} AND r.next_followup_at IS NOT NULL AND substr(r.next_followup_at,1,10) < ?",
        [*base_params, today])
    calls_due_today = _scalar(
        f"SELECT COUNT(*) AS n FROM crm_company_relationships r WHERE r.organization_id=? "
        f"{assigned_filter} AND r.next_followup_at IS NOT NULL AND substr(r.next_followup_at,1,10) = ?",
        [*base_params, today])
    new_assignments = _scalar(
        f"SELECT COUNT(*) AS n FROM crm_company_relationships r WHERE r.organization_id=? "
        f"{assigned_filter} AND r.relationship_status='assigned'",
        base_params)
    rel_join, rel_rank, rel_params = trust.relevance_sql("r.company_id")
    high_priority_opps = _scalar(
        f"SELECT COUNT(*) AS n FROM crm_company_relationships r {rel_join} "
        f"WHERE r.organization_id=? {assigned_filter} AND {rel_rank} = {trust.RANK[trust.CORE]}",
        [*rel_params, *base_params])
    quotes_requested = _scalar(
        f"SELECT COUNT(*) AS n FROM crm_company_relationships r WHERE r.organization_id=? "
        f"{assigned_filter} AND r.relationship_status IN ('quote_requested','quote_sent')",
        base_params)
    appointments = _scalar(
        "SELECT COUNT(*) AS n FROM crm_activities WHERE organization_id=? AND user_id=? "
        "AND activity_outcome='appointment_set'", [org_id, uid])
    tasks_due = _scalar(
        "SELECT COUNT(*) AS n FROM crm_tasks WHERE organization_id=? AND assigned_user_id=? "
        "AND status IN ('open','in_progress') AND due_at IS NOT NULL AND substr(due_at,1,10) <= ?",
        [org_id, uid, today])

    recent_activity = [dict(r) for r in conn.execute(
        "SELECT a.id, a.company_id, c.display_name, a.activity_type, a.activity_outcome, "
        "a.subject, a.activity_at FROM crm_activities a JOIN companies c ON c.id=a.company_id "
        "WHERE a.organization_id=? AND a.user_id=? ORDER BY a.activity_at DESC LIMIT 10",
        (org_id, uid))]

    # Priority companies: worked relationships ordered by account relevance.
    # Accounts outside the plumbing-supply focus stay in the full list but are
    # never promoted here.
    priority = list_my_companies(conn, user, {"page_size": 8, "relevance": "promotable"})["items"][:8]
    today_accounts = _todays_accounts(conn, user, scoped=scoped)

    return {
        "scope": "organization" if scoped else "assignment",
        "assignment_scoped": not scoped,
        "kpis": {
            "calls_due_today": calls_due_today,
            "overdue_followups": overdue_followups,
            "new_assigned_companies": new_assignments,
            "high_priority_opportunities": high_priority_opps,
            "appointments_scheduled": appointments,
            "quotes_requested": quotes_requested,
            "tasks_due_today": tasks_due,
            "my_companies": my_companies,
            "accounts_to_act_on_today": len(today_accounts["items"]),
        },
        # Back-compat top-level keys.
        "my_companies": my_companies,
        "overdue_followups": overdue_followups,
        "new_assignments": new_assignments,
        "tasks_due_today": tasks_due,
        "recent_activity": recent_activity,
        "high_priority_companies": priority,
        "priority_companies": priority,
        "todays_accounts": today_accounts,
        "followups_due": followups_due(conn, user, limit=10),
        "recent_opportunity_activity": recent_opportunity_activity(conn, user, limit=8),
        "activity_summary": activity_summary(conn, user),
    }


def _todays_accounts(conn: sqlite3.Connection, user: dict, *, scoped: bool) -> dict:
    """Trust-gated accounts for today. Reps see only accounts assigned to them;
    org-scoped roles see the whole queue. Do-not-contact relationships are removed.
    Never creates CRM rows."""
    org_id = user["organization_id"]
    dnc = {r["company_id"] for r in conn.execute(
        "SELECT company_id FROM crm_company_relationships WHERE organization_id=? "
        "AND (do_not_contact=1 OR relationship_status='do_not_contact')", (org_id,))}
    ids = None
    if not scoped:
        ids = {r["company_id"] for r in conn.execute(
            "SELECT company_id FROM crm_company_relationships WHERE organization_id=? "
            "AND assigned_user_id=?", (org_id, user["id"]))}
    try:
        return trust.todays_accounts(conn, company_ids=ids, exclude_ids=dnc)
    except sqlite3.Error:
        return {"items": [], "data_status": {"refresh": "Unknown", "note": "Account checks unavailable."},
                "note": "Account checks are unavailable right now."}


def followups_due(conn: sqlite3.Connection, user: dict, *, limit: int = 50,
                  include_future: bool = False) -> list[dict]:
    """Companies with a follow-up due (or overdue). Record-level scoped."""
    require_permission(user, "crm.relationships.view")
    org_id = user["organization_id"]
    where = ["r.organization_id=?", "r.next_followup_at IS NOT NULL"]
    params: list = [org_id]
    if not has_permission(user, "companies.view"):
        where.append("r.assigned_user_id=?")
        params.append(user["id"])
    if not include_future:
        where.append("substr(r.next_followup_at,1,10) <= ?")
        params.append(_today())
    rows = conn.execute(
        f"""
        SELECT r.company_id, c.display_name, r.relationship_status,
               r.next_followup_at, r.last_contact_at, r.assigned_user_id,
               u.display_name AS assigned_to,
               (SELECT activity_outcome FROM crm_activities a
                WHERE a.company_id=r.company_id AND a.organization_id=r.organization_id
                ORDER BY a.activity_at DESC LIMIT 1) AS last_outcome
        FROM crm_company_relationships r
        JOIN companies c ON c.id=r.company_id
        LEFT JOIN users u ON u.id=r.assigned_user_id
        WHERE {' AND '.join(where)}
        ORDER BY r.next_followup_at ASC LIMIT ?
        """,
        [*params, limit]).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["overdue"] = _followup_overdue(r["next_followup_at"])
        d["recommended_action"] = recommended_action(
            r["relationship_status"], r["next_followup_at"], r["last_contact_at"])
        out.append(d)
    return out


def recent_opportunity_activity(conn: sqlite3.Connection, user: dict, *,
                                limit: int = 20) -> list[dict]:
    """Recently active projects tied to the user's assigned companies."""
    require_permission(user, "crm.relationships.view")
    org_id = user["organization_id"]
    where = ["r.organization_id=?"]
    params: list = [org_id]
    if not has_permission(user, "companies.view"):
        where.append("r.assigned_user_id=?")
        params.append(user["id"])
    rows = conn.execute(
        f"""
        SELECT pr.id AS project_id, r.company_id, c.display_name,
               p.jurisdiction, p.job_address, p.city, pr.project_lifecycle,
               pr.opportunity_score, pr.opportunity_date, pr.opportunity_timing,
               pr.project_category, p.permit_type, p.permit_subtype, p.description,
               p.project_description
        FROM crm_company_relationships r
        JOIN companies c ON c.id=r.company_id
        JOIN projects pr ON pr.contractor_company_id=c.id
        JOIN permits p ON p.id=pr.permit_id
        WHERE {' AND '.join(where)}
        ORDER BY pr.opportunity_date DESC LIMIT ?
        """,
        [*params, limit * 20]).fetchall()
    return trust.annotate_projects([dict(r) for r in rows], conn=conn)[:limit]


def activity_summary(conn: sqlite3.Connection, user: dict, *, days: int = 30) -> dict:
    """Personal activity counts (this user) over a trailing window."""
    org_id, uid = user["organization_id"], user["id"]
    cutoff = (datetime.now(timezone.utc)).strftime("%Y-%m-%d")

    def _n(sql, extra=()):
        return conn.execute(sql, (org_id, uid, *extra)).fetchone()["n"]

    return {
        "calls_logged": _n("SELECT COUNT(*) n FROM crm_activities WHERE organization_id=? "
                           "AND user_id=? AND activity_type IN ('call','voicemail')"),
        "conversations": _n("SELECT COUNT(*) n FROM crm_activities WHERE organization_id=? "
                            "AND user_id=? AND activity_outcome='spoke_with_contact'"),
        "appointments": _n("SELECT COUNT(*) n FROM crm_activities WHERE organization_id=? "
                           "AND user_id=? AND activity_outcome='appointment_set'"),
        "quote_requests": _n("SELECT COUNT(*) n FROM crm_activities WHERE organization_id=? "
                             "AND user_id=? AND activity_type IN ('quote_request','quote_sent')"),
        "followups_completed": _n("SELECT COUNT(*) n FROM crm_tasks WHERE organization_id=? "
                                  "AND assigned_user_id=? AND status='completed'"),
        "companies_contacted": _n("SELECT COUNT(DISTINCT company_id) n FROM crm_activities "
                                  "WHERE organization_id=? AND user_id=?"),
    }


# Upper bound on project rows scanned when filtering by trade scope in Python.
_SCOPE_SCAN_LIMIT = 5000

OPPORTUNITY_CONTEXTS = ("assigned", "organization")


def opportunities(conn: sqlite3.Connection, user: dict, filters: dict | None = None,
                  *, all_rows: bool = False) -> dict:
    """Projects in an explicit context, ranked by opportunity score.

    context=assigned (default): projects of companies with a CRM relationship
    (reps: only their own). context=organization: every project in the recent
    trust window, attributed or not; needs companies.view. Both contexts apply
    the same trust-layer project checks; scope=all shows everything, labelled.
    The dashboard's Top Opportunities is page 1 of this same call, and the
    opportunity map is all_rows=True of it (every row, unpaged, same filters)."""
    require_permission(user, "projects.view_assigned")
    from pipeline.config import settings as _s
    filters = filters or {}
    context = (filters.get("context") or "assigned").strip().lower()
    if context not in OPPORTUNITY_CONTEXTS:
        raise ValidationError("unknown opportunities context")
    if context == "organization" and not has_permission(user, "companies.view"):
        raise AuthzError("organization opportunities require companies.view")
    org_id = user["organization_id"]
    extra, extra_params = [], []
    q = (filters.get("q") or "").strip()
    if q:
        extra.append("(c.display_name LIKE ? OR p.job_address LIKE ? OR p.jurisdiction LIKE ?)")
        extra_params += [f"%{q}%", f"%{q}%", f"%{q}%"]
    lifecycle = (filters.get("lifecycle") or "").strip()
    if lifecycle:
        extra.append("pr.project_lifecycle=?")
        extra_params.append(lifecycle)
    smin = filters.get("score_min")
    if smin not in (None, ""):
        extra.append("COALESCE(pr.opportunity_score,0) >= ?")
        extra_params.append(float(smin))
    # Projects that name only a non-wet trade, or belong to an account outside the
    # plumbing-supply focus, are hidden unless scope=all. Ranking is unchanged.
    include_all = str(filters.get("scope") or "").lower() == "all"

    if context == "assigned":
        where = ["r.organization_id=?"]
        params: list = [org_id]
        if not has_permission(user, "companies.view"):
            where.append("r.assigned_user_id=?")
            params.append(user["id"])
        source = ("FROM crm_company_relationships r JOIN companies c ON c.id=r.company_id "
                  "JOIN projects pr ON pr.contractor_company_id=c.id JOIN permits p ON p.id=pr.permit_id")
        company_col = "r.company_id"
    else:
        where = [f"substr(COALESCE(NULLIF(TRIM(p.issued_date),''), p.filed_date, pr.opportunity_date),1,10) >= ?"]
        params = [_window_start()]
        source = ("FROM projects pr JOIN permits p ON p.id=pr.permit_id "
                  "LEFT JOIN companies c ON c.id=COALESCE(pr.contractor_company_id, p.contractor_company_id)")
        company_col = "c.id"
    where_sql = " AND ".join(where + extra)
    all_params = [*params, *extra_params]
    select_sql = f"""
        SELECT pr.id AS project_id, {company_col} AS company_id, c.display_name,
               p.permit_number, p.jurisdiction, p.job_address, p.city, p.state,
               p.description, p.project_description, p.permit_type, p.permit_subtype,
               pr.project_category, pr.project_lifecycle,
               pr.opportunity_score, pr.opportunity_date, pr.opportunity_timing,
               pr.estimated_material_value, p.latitude, p.longitude, p.raw_source_json
        {source}
        WHERE {where_sql}
        ORDER BY COALESCE(pr.opportunity_score,0) DESC, pr.opportunity_date DESC
        LIMIT ? OFFSET ?
        """

    # The count and the candidate scan are shared per data signature: the list
    # and the map (requested together) run them once. The key holds everything
    # the scan reads, so a cached result equals a fresh one.
    sig = scan_cache.data_signature(conn, org_id)
    scope_key = (context, org_id, None if has_permission(user, "companies.view") else user["id"],
                 where_sql, tuple(all_params), include_all)

    def _count() -> int:
        return conn.execute(f"SELECT COUNT(*) AS n {source} WHERE {where_sql}", all_params).fetchone()["n"]

    def _scan() -> tuple:
        rows = conn.execute(select_sql, [*all_params, _SCOPE_SCAN_LIMIT, 0]).fetchall()
        scanned = trust.annotate_projects([dict(r) for r in rows], include_all=include_all, conn=conn)
        _attach_locations(scanned)
        return tuple(scanned)

    total = scan_cache.get_or_compute(None if sig is None else ("count", sig, scope_key), _count)
    before_checks = total
    page = max(1, int(filters.get("page", 1) or 1))
    try:
        page_size = int(filters.get("page_size") or _s.CRM_PAGE_SIZE_DEFAULT)
    except (TypeError, ValueError):
        page_size = _s.CRM_PAGE_SIZE_DEFAULT
    page_size = max(1, min(page_size, _s.CRM_PAGE_SIZE_MAX))
    if include_all and not all_rows:
        # scope=all list pages: SQL does the paging; only this page is read.
        rows = conn.execute(select_sql, [*all_params, page_size, (page - 1) * page_size]).fetchall()
        items = trust.annotate_projects([dict(r) for r in rows], include_all=include_all, conn=conn)
        _attach_locations(items)
    else:
        scanned = scan_cache.get_or_compute(None if sig is None else ("scan", sig, scope_key), _scan)
        # With scope=all the total is the SQL count; otherwise it is what passed the
        # checks among the scanned candidates. all_rows never changes the total.
        if not include_all:
            total = len(scanned)
        chosen = scanned if all_rows else scanned[(page - 1) * page_size: page * page_size]
        items = [dict(i) for i in chosen]  # per-request copies; the cache is never mutated
    # When candidates exceed the scan cap, only the top-scoring ones are checked
    # (default) or loaded (all_rows). Say so instead of implying completeness.
    scan_limited = before_checks > _SCOPE_SCAN_LIMIT and (not include_all or all_rows)
    _attach_permit_counts_and_contacts(conn, items, permit_counts=not all_rows)
    reason, message = _empty_reason(conn, user, context, filtered=bool(extra), total=total,
                                    before_checks=before_checks, include_all=include_all)
    hidden = before_checks - total if not include_all and before_checks <= _SCOPE_SCAN_LIMIT else None
    return {"items": items, "total": total, "page": page, "context": context,
            "include_all_scopes": include_all, "hidden_by_checks": hidden,
            "candidates": before_checks, "scan_limit": _SCOPE_SCAN_LIMIT if scan_limited else None,
            "empty_reason": reason, "empty_message": message,
            "page_size": page_size, "pages": (total + page_size - 1) // page_size if page_size else 1}


def opportunity_map(conn: sqlite3.Connection, user: dict, filters: dict | None = None) -> dict:
    """Map layer for the opportunity feed. Same function, same filters, same
    trust checks as opportunities(); only the rows with a source-published
    location are placed. Unplaced rows are counted by jurisdiction, never
    guessed onto the map."""
    from collections import Counter

    feed = opportunities(conn, user, filters, all_rows=True)
    points, unplaced = [], Counter()
    for it in feed["items"]:
        loc = it.get("location")
        if loc is None:
            unplaced[it.get("jurisdiction") or "unknown"] += 1
            continue
        c = it.get("contact") or {}
        points.append({
            "project_id": it["project_id"], "company_id": it.get("company_id"),
            "display_name": it.get("display_name"), "permit_number": it.get("permit_number"),
            "job_address": it.get("job_address"), "jurisdiction": it.get("jurisdiction"),
            "trade_scope": it.get("trade_scope"), "opportunity_score": it.get("opportunity_score"),
            "opportunity_date": it.get("opportunity_date"),
            "account_relevance": it.get("account_relevance"),
            "contact": {"status": c.get("status"), "label": c.get("label"),
                        "name": c.get("name"), "phone": c.get("phone"), "email": c.get("email")},
            "lat": loc["lat"], "lon": loc["lon"], "precision": loc["precision"],
        })
    # total is always the feed's total; placed + unplaced + not_loaded == total.
    return {
        "context": feed["context"], "include_all_scopes": feed["include_all_scopes"],
        "total": feed["total"], "placed": len(points), "unplaced": sum(unplaced.values()),
        "not_loaded": feed["total"] - len(feed["items"]),
        "candidates": feed["candidates"], "scan_limit": feed["scan_limit"],
        "unplaced_by_jurisdiction": dict(unplaced.most_common()),
        "points": points,
        "empty_reason": feed["empty_reason"], "empty_message": feed["empty_message"],
        "location_note": ("Only coordinates published by the permit source are mapped; permits "
                          "without them are listed but not placed."),
    }


def _window_start() -> str:
    from datetime import timedelta

    from pipeline.trust.gates import REVIEW_WINDOW_DAYS
    return (datetime.now(timezone.utc) - timedelta(days=REVIEW_WINDOW_DAYS)).strftime("%Y-%m-%d")


def _attach_locations(items: list[dict]) -> None:
    """Replace raw coordinate columns with the source-published location, or
    None. Raw source JSON never leaves the server."""
    from pipeline.trust.geo import permit_location
    for it in items:
        loc = permit_location(it)
        for k in ("latitude", "longitude", "raw_source_json"):
            it.pop(k, None)
        it["location"] = loc


def _attach_permit_counts_and_contacts(conn: sqlite3.Connection, items: list[dict],
                                       *, permit_counts: bool = True) -> None:
    """Per page only. The contact summary is the same verified view the company
    page shows, so a list row can never claim a contact the page does not have."""
    cache: dict[int, dict] = {}
    for it in items:
        cid = it.get("company_id")
        if cid is None:
            it["permit_count"] = None
            it["contact"] = {"status": "NO_COMPANY", "label": "No contractor attributed to this permit"}
            continue
        it["permit_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM permits WHERE contractor_company_id=?",
            (cid,)).fetchone()["n"] if permit_counts else None
        if cid not in cache:
            v = verified_contact_view(conn, cid)
            verified = v["status"] == "VERIFIED"
            cache[cid] = {
                "status": v["status"],
                "label": v["status_label"],
                "name": v["contact_name"] if verified else None,
                "title": v["title"] if verified else None,
                "phone": v["phone"]["value"] if verified and v["phone"] else None,
                "email": v["email"]["value"] if verified and v["email"] else None,
            }
        it["contact"] = cache[cid]


def _empty_reason(conn, user, context, *, filtered, total, before_checks, include_all):
    if total:
        return None, None
    if context == "assigned":
        where, params = "organization_id=?", [user["organization_id"]]
        if not has_permission(user, "companies.view"):
            where += " AND assigned_user_id=?"
            params.append(user["id"])
        n_rel = conn.execute(f"SELECT COUNT(*) AS n FROM crm_company_relationships WHERE {where}",
                             params).fetchone()["n"]
        if n_rel == 0:
            if has_permission(user, "companies.view"):
                return "NO_RELATIONSHIPS", ("No companies have CRM relationships yet, so the assigned view is empty. "
                                            "Switch to the organization view to see all current opportunities.")
            return "NO_ASSIGNED_COMPANIES", ("No companies are assigned to you yet. "
                                             "Opportunities appear here for companies you are assigned.")
    if filtered:
        return "FILTERED", "No opportunities match the current search or filters."
    if before_checks and not include_all:
        scope = "these companies" if context == "assigned" else "the last 60 days"
        return "NONE_PASS_CHECKS", (
            f"{before_checks} project{'s' if before_checks != 1 else ''} in {scope} did not pass the "
            "plumbing-supply checks (the permit names only non-wet work, or the account is outside the "
            "plumbing-supply focus). Choose \"Include all scopes\" to review them.")
    return "NO_PROJECTS", ("There are no current projects for these companies." if context == "assigned"
                           else "There are no projects in the last 60 days.")


def my_activity(conn: sqlite3.Connection, user: dict, filters: dict | None = None) -> dict:
    """The current user's CRM activity feed (paginated)."""
    require_permission(user, "crm.activities.view")
    from pipeline.config import settings as _s
    filters = filters or {}
    org_id, uid = user["organization_id"], user["id"]
    where = ["a.organization_id=?", "a.user_id=?"]
    params: list = [org_id, uid]
    atype = (filters.get("activity_type") or "").strip()
    if atype:
        where.append("a.activity_type=?")
        params.append(atype)
    where_sql = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM crm_activities a WHERE {where_sql}", params).fetchone()["n"]
    page = max(1, int(filters.get("page", 1) or 1))
    try:
        page_size = int(filters.get("page_size") or _s.CRM_PAGE_SIZE_DEFAULT)
    except (TypeError, ValueError):
        page_size = _s.CRM_PAGE_SIZE_DEFAULT
    page_size = max(1, min(page_size, _s.CRM_PAGE_SIZE_MAX))
    rows = conn.execute(
        f"""
        SELECT a.id, a.company_id, c.display_name, a.activity_type, a.activity_outcome,
               a.subject, a.notes, a.activity_at, a.next_followup_at, a.project_id
        FROM crm_activities a JOIN companies c ON c.id=a.company_id
        WHERE {where_sql}
        ORDER BY a.activity_at DESC, a.id DESC LIMIT ? OFFSET ?
        """,
        [*params, page_size, (page - 1) * page_size]).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "page_size": page_size, "pages": (total + page_size - 1) // page_size if page_size else 1}
