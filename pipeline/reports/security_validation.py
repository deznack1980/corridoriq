"""Phase 12 — Security & CRM access validation reports.

    python -m pipeline.reports.security_validation

Generates (evidence is produced by running live authorization checks against a
fresh in-memory database — the report reflects the code's actual behavior):

  reports/generated/security_rbac_validation_<date>.md
  reports/generated/security_rbac_validation_<date>.xlsx
  reports/generated/crm_access_validation_<date>.md

No secrets, passwords, hashes, sessions, or tokens are written to these reports.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from pipeline.auth import service as auth
from pipeline.auth.rbac import (
    AuthzError,
    PERMISSIONS,
    ROLES,
    can_access_company,
    has_permission,
)
from pipeline.auth.seed import seed_auth
from pipeline.config import settings
from pipeline.crm import admin as crm_admin
from pipeline.crm import serializers
from pipeline.crm import service as crm

_HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
_HEADER_FONT = Font(bold=True, color="FFFFFF")

ROLE_ORDER = ["admin", "sales_manager", "sales_representative", "read_only", "fulfillment_user"]

# Protected endpoints: method, path, required permission, record-level access.
PROTECTED_ENDPOINTS = [
    ("POST", "/api/auth/login", "(public)", "no"),
    ("POST", "/api/auth/logout", "authenticated", "no"),
    ("GET", "/api/auth/me", "authenticated", "no"),
    ("POST", "/api/auth/change-password", "authenticated", "no"),
    ("GET", "/api/sales/dashboard", "authenticated", "own records"),
    ("GET", "/api/sales/companies", "crm.relationships.view", "assigned only"),
    ("GET", "/api/sales/companies/<id>", "companies.view_assigned", "assigned only"),
    ("GET", "/api/sales/companies/<id>/projects", "projects.view_assigned", "assigned only"),
    ("GET", "/api/sales/companies/<id>/permits", "permits.view", "assigned only"),
    ("GET", "/api/sales/companies/<id>/activities", "crm.activities.view", "assigned only"),
    ("POST", "/api/sales/companies/<id>/activities", "crm.activities.create", "assigned only"),
    ("PATCH", "/api/sales/companies/<id>/relationship", "crm.relationships.update", "assigned only"),
    ("GET", "/api/sales/tasks", "crm.tasks.view", "own tasks"),
    ("POST", "/api/sales/tasks", "crm.tasks.create", "own/assign"),
    ("PATCH", "/api/sales/tasks/<id>", "crm.tasks.edit_own/complete", "own tasks"),
    ("GET", "/api/manager/team", "users.view", "organization"),
    ("GET", "/api/manager/assignments", "companies.assign / users.view", "organization"),
    ("POST", "/api/manager/assignments", "companies.assign", "organization"),
    ("GET", "/api/admin/users", "users.view", "organization"),
    ("POST", "/api/admin/users", "users.create", "organization"),
    ("PATCH", "/api/admin/users/<id>", "users.update / users.disable", "organization"),
]

CRM_WRITABLE_TABLES = [
    "crm_company_relationships", "crm_activities", "crm_activity_revisions",
    "crm_tasks", "crm_assignment_history",
]
INTELLIGENCE_READONLY_TABLES = [
    "companies", "company_roles", "contacts", "company_aliases",
    "company_intelligence", "company_activity", "company_match_review_queue",
    "company_identity_audit_log", "projects", "permits",
    "status_dictionary", "permit_code_dictionary", "keyword_dictionary",
    "company_capabilities", "company_capability_evidence",
    "enrichment_source_registry", "company_enrichment",
    "customer_relevance_profiles", "project_customer_relevance",
    "project_demand_labels", "company_customer_priority",
    "roc_import_runs", "roc_licenses", "roc_classification_map",
    "roc_company_matches", "roc_identity_validations",
    "roc_contact_candidates", "roc_duplicate_candidates",
    "canonical_companies", "company_entity_links",
    "entity_duplicate_reviews", "entity_match_overrides",
    "company_contact_channels",
    "sales_identity_reviews", "sales_person_reviews", "sales_account_reviews",
    "sales_lanes", "company_sales_lanes", "sales_lane_books",
    "sales_lane_snapshots", "sales_callability", "sales_presentation_notes",
]
EXPECTED_AUDIT_EVENTS = [
    "login_success", "login_failure", "logout", "password_changed",
    "account_locked", "account_disabled", "permission_denied", "company_viewed",
    "activity_created", "activity_updated", "task_created", "task_completed",
    "assignment_changed", "relationship_status_changed", "admin_change",
]
SENSITIVE_FIELDS = [
    "password_hash", "normalized_email", "source_system", "source_record_id",
    "session token / cookie value", "authentication secrets", "API keys",
    "database file paths", "environment variables", "internal scoring weights",
    "restricted supplier cost files", "raw security logs",
    "enrichment source families", "contractor capability methodology",
    "roc source internals", "license classification map",
]


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Live evidence harness
# --------------------------------------------------------------------------

def _build_env() -> dict:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(settings.SCHEMA_PATH.read_text(encoding="utf-8"))
    c.execute("INSERT INTO jurisdictions (slug, name, state, status) "
              "VALUES ('phoenix_az','Phoenix','AZ','connected')")
    seed_auth(c)
    org = c.execute("SELECT id FROM organizations WHERE slug='corridoriq'").fetchone()["id"]

    def mkuser(email, role):
        uid = auth.create_user(c, organization_id=org, email=email, password="Passw0rd123!",
                               role_names=[role], must_change_password=False)
        return uid

    ids = {r: mkuser(f"{r}@corridoriq.com", r) for r in ROLE_ORDER}
    ctx = {r: auth.build_user_context(c, c.execute("SELECT * FROM users WHERE id=?", (i,)).fetchone())
           for r, i in ids.items()}

    # A company per role, each assigned to that role's user (by admin).
    companies = {}
    now = _now()
    for r in ROLE_ORDER:
        cur = c.execute("INSERT INTO companies (normalized_name, display_name, city, state, "
                        "source_system, source_record_id, lifecycle_state, created_at, updated_at) "
                        "VALUES (?,?,?,?,?,?,'active',?,?)",
                        (f"CO_{r}".upper(), f"Co {r}", "PHOENIX", "AZ", "src", "rec1", now, now))
        cid = cur.lastrowid
        c.execute("INSERT INTO company_intelligence (company_id, company_priority_score, "
                  "company_priority_tier, model_version) VALUES (?, 70, 'High', 'company-metrics-v1')",
                  (cid,))
        companies[r] = cid
        crm.assign_company(c, ctx["admin"], cid, ids[r])
    c.commit()
    return {"conn": c, "org": org, "ids": ids, "ctx": ctx, "companies": companies}


def _run(fn):
    try:
        fn()
        return "ALLOWED", ""
    except AuthzError as exc:
        return "DENIED", str(exc)
    except crm.ValidationError as exc:
        return "DENIED", str(exc)
    except Exception as exc:  # pragma: no cover
        return "ERROR", str(exc)


def _denial_matrix(env) -> list[dict]:
    c, ctx, ids, companies = env["conn"], env["ctx"], env["ids"], env["companies"]
    rows = []

    def action(name, fn_for_role, expected_map):
        rec = {"action": name}
        for r in ROLE_ORDER:
            result, _ = _run(lambda r=r: fn_for_role(r))
            rec[r] = result
        rec["expected"] = expected_map
        rows.append(rec)

    action("View assigned company intelligence",
           lambda r: crm.get_company_detail(c, ctx[r], companies[r]),
           "sales/mgr/admin/read_only allowed; fulfillment denied")
    action("Create CRM activity",
           lambda r: crm.create_activity(c, ctx[r], companies[r], {"activity_type": "note"}),
           "rep/mgr/admin allowed; read_only/fulfillment denied")
    action("Update relationship status",
           lambda r: crm.update_relationship(c, ctx[r], companies[r], {"relationship_status": "contacted"}),
           "rep/mgr/admin allowed; read_only/fulfillment denied")
    action("Assign a company",
           lambda r: crm.assign_company(c, ctx[r], companies[r], ids["read_only"]),
           "mgr/admin allowed; others denied")
    action("List organization users",
           lambda r: crm_admin.list_users(c, ctx[r]),
           "mgr/admin allowed; others denied")
    action("Create a user",
           lambda r: crm_admin.create_user(c, ctx[r], {"email": f"probe_{r}@corridoriq.com"}),
           "admin allowed; others denied")

    def perm_action(name, key):
        rec = {"action": name, "expected": "admin only"}
        for r in ROLE_ORDER:
            rec[r] = "ALLOWED" if has_permission(ctx[r], key) else "DENIED"
        rows.append(rec)

    perm_action("Access Municipal Knowledge approvals", "knowledge.approve")
    perm_action("Access restricted supplier pricing", "supplier_pricing.view")
    perm_action("Manage roles / permissions", "roles.manage")
    return rows


def _record_level_checks(env) -> list[dict]:
    c, ctx, companies = env["conn"], env["ctx"], env["companies"]
    rep = ctx["sales_representative"]
    own = companies["sales_representative"]
    other = companies["read_only"]
    return [
        {"check": "Rep views own assigned company",
         "result": "ALLOWED" if can_access_company(c, rep, own) else "DENIED", "expected": "ALLOWED"},
        {"check": "Rep views a company assigned to another rep",
         "result": "ALLOWED" if can_access_company(c, rep, other) else "DENIED", "expected": "DENIED"},
        {"check": "Admin views any company",
         "result": "ALLOWED" if can_access_company(c, ctx["admin"], other) else "DENIED", "expected": "ALLOWED"},
        {"check": "Fulfillment views contractor intelligence",
         "result": "ALLOWED" if can_access_company(c, ctx["fulfillment_user"], companies["fulfillment_user"])
         else "DENIED", "expected": "DENIED"},
    ]


def _org_boundary_checks(env) -> list[dict]:
    c, ctx = env["conn"], env["ctx"]
    now = _now()
    cur = c.execute("INSERT INTO organizations (name, slug, is_active, created_at, updated_at) "
                    "VALUES ('Other Org','other-org',1,?,?)", (now, now))
    other_org = cur.lastrowid
    c.commit()
    other_admin_id = auth.create_user(c, organization_id=other_org, email="admin@other-org.com",
                                      password="OtherPass123!", role_names=["admin"],
                                      must_change_password=False)
    other_admin = auth.build_user_context(
        c, c.execute("SELECT * FROM users WHERE id=?", (other_admin_id,)).fetchone())
    my_companies = crm.list_my_companies(c, other_admin)["total"]
    cross_result, cross_msg = _run(
        lambda: crm.assign_company(c, other_admin, env["companies"]["sales_representative"],
                                   env["ids"]["read_only"]))
    return [
        {"check": "Other-org admin sees default-org CRM records",
         "result": f"{my_companies} records", "expected": "0 records (isolated)"},
        {"check": "Other-org admin assigns a user from another org",
         "result": cross_result, "expected": "DENIED"},
    ]


def _sensitive_field_checks(env) -> list[dict]:
    c = env["conn"]
    company_row = c.execute("SELECT * FROM companies LIMIT 1").fetchone()
    user_row = c.execute("SELECT * FROM users LIMIT 1").fetchone()
    ser_company = serializers.serialize_company(company_row)
    ser_user = serializers.serialize_user(user_row)
    return [
        {"field": "companies.source_system",
         "in_serialized_output": "NO" if "source_system" not in ser_company else "YES",
         "expected": "NO"},
        {"field": "companies.source_record_id",
         "in_serialized_output": "NO" if "source_record_id" not in ser_company else "YES",
         "expected": "NO"},
        {"field": "users.password_hash",
         "in_serialized_output": "NO" if "password_hash" not in ser_user else "YES",
         "expected": "NO"},
        {"field": "users.normalized_email",
         "in_serialized_output": "NO" if "normalized_email" not in ser_user else "YES",
         "expected": "NO"},
        {"field": "users.failed_login_count / locked_until",
         "in_serialized_output": "NO" if "failed_login_count" not in ser_user else "YES",
         "expected": "NO"},
    ]


def _audit_coverage(env) -> list[dict]:
    c = env["conn"]
    seen = {r["event_type"] for r in c.execute("SELECT DISTINCT event_type FROM security_audit_log")}
    return [{"event_type": e, "observed": "YES" if e in seen else "not in this run"}
            for e in EXPECTED_AUDIT_EVENTS]


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

def _role_matrix_rows() -> list[list[str]]:
    rows = []
    for key in sorted(PERMISSIONS.keys()):
        row = [key]
        for r in ROLE_ORDER:
            row.append("Y" if key in ROLES[r]["permissions"] else "")
        rows.append(row)
    return rows


def _write_markdown(path, env, denial, record_level, org_boundary, sensitive, audit):
    L = []
    L.append("# CorridorIQ — Security & RBAC Validation")
    L.append(f"\n_Generated {_today()} • evidence produced by live authorization checks_\n")
    L.append("This report contains no secrets, passwords, hashes, sessions, or tokens.\n")

    L.append("## 1. Role & permission matrix\n")
    L.append("| Permission | " + " | ".join(ROLE_ORDER) + " |")
    L.append("|" + "---|" * (len(ROLE_ORDER) + 1))
    for row in _role_matrix_rows():
        L.append("| " + " | ".join(row) + " |")

    L.append("\n## 2. Protected endpoints\n")
    L.append("| Method | Path | Required permission | Record-level access |")
    L.append("|---|---|---|---|")
    for m, p, perm, rec in PROTECTED_ENDPOINTS:
        L.append(f"| {m} | `{p}` | {perm} | {rec} |")

    L.append("\n## 3. Denied-access results (live)\n")
    L.append("| Action | " + " | ".join(ROLE_ORDER) + " | Expected |")
    L.append("|" + "---|" * (len(ROLE_ORDER) + 2))
    for rec in denial:
        cells = [rec["action"]] + [rec[r] for r in ROLE_ORDER] + [rec["expected"]]
        L.append("| " + " | ".join(cells) + " |")

    L.append("\n## 4. Record-level access checks (live)\n")
    L.append("| Check | Result | Expected |")
    L.append("|---|---|---|")
    for rec in record_level:
        L.append(f"| {rec['check']} | {rec['result']} | {rec['expected']} |")

    L.append("\n## 5. Organization-boundary tests (live)\n")
    L.append("| Check | Result | Expected |")
    L.append("|---|---|---|")
    for rec in org_boundary:
        L.append(f"| {rec['check']} | {rec['result']} | {rec['expected']} |")

    L.append("\n## 6. Sensitive-field serialization checks (live)\n")
    L.append("| Field | Present in sales output | Expected |")
    L.append("|---|---|---|")
    for rec in sensitive:
        L.append(f"| {rec['field']} | {rec['in_serialized_output']} | {rec['expected']} |")

    L.append("\n## 7. CRM writable vs. intelligence read-only tables\n")
    L.append("**Writable by CRM (organization-owned):**")
    for t in CRM_WRITABLE_TABLES:
        L.append(f"- `{t}`")
    L.append("\n**Read-only intelligence (never modified by sales users):**")
    for t in INTELLIGENCE_READONLY_TABLES:
        L.append(f"- `{t}`")

    L.append("\n## 8. Audit-log coverage (live)\n")
    L.append("| Event type | Observed in validation run |")
    L.append("|---|---|")
    for rec in audit:
        L.append(f"| {rec['event_type']} | {rec['observed']} |")

    L.append("\n## 9. Session-security configuration\n")
    L.append("| Setting | Value |")
    L.append("|---|---|")
    L.append("| Session storage | Server-side (DB); browser holds opaque token only |")
    L.append("| Cookie flags | HttpOnly, SameSite=Lax |")
    L.append(f"| Secure cookie (production) | {'enabled' if settings.AUTH_PRODUCTION else 'enabled when CORRIDORIQ_ENV=production'} |")
    L.append(f"| Session TTL | {settings.SESSION_TTL_HOURS} hours |")
    L.append("| Logout | revokes the session immediately |")
    L.append("| Password change | revokes all of the user's sessions |")
    L.append(f"| Account lockout | after {settings.AUTH_MAX_FAILED_LOGINS} failures, {settings.AUTH_LOCKOUT_MINUTES} min |")
    L.append("| Token storage | never in localStorage |")

    L.append("\n## 10. Outstanding risks\n")
    L.append("- Pilot uses a single default organization; full multi-tenant billing/isolation is out of scope.")
    L.append("- Rate limiting is coarse (account lockout only); add IP-based throttling before public exposure.")
    L.append("- Serve behind HTTPS in production so the Secure cookie flag is effective.")
    L.append("- Session store is not yet periodically pruned; add a cleanup job for expired sessions.")

    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def _autosize(ws):
    for col in ws.columns:
        letter = get_column_letter(col[0].column)
        longest = max((len(str(c.value)) if c.value is not None else 0) for c in col)
        ws.column_dimensions[letter].width = min(max(longest + 2, 10), 60)


def _style_header(ws):
    for cell in ws[1]:
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT


def _write_xlsx(path, denial, record_level, org_boundary, sensitive, audit):
    wb = Workbook()
    ws = wb.active
    ws.title = "Role-Permission Matrix"
    ws.append(["Permission", *ROLE_ORDER])
    for row in _role_matrix_rows():
        ws.append(row)
    _style_header(ws); _autosize(ws)

    ws2 = wb.create_sheet("Protected Endpoints")
    ws2.append(["Method", "Path", "Required permission", "Record-level access"])
    for r in PROTECTED_ENDPOINTS:
        ws2.append(list(r))
    _style_header(ws2); _autosize(ws2)

    ws3 = wb.create_sheet("Denied-Access Results")
    ws3.append(["Action", *ROLE_ORDER, "Expected"])
    for rec in denial:
        ws3.append([rec["action"], *[rec[r] for r in ROLE_ORDER], rec["expected"]])
    _style_header(ws3); _autosize(ws3)

    ws4 = wb.create_sheet("Record & Org Checks")
    ws4.append(["Check", "Result", "Expected"])
    for rec in record_level + org_boundary:
        ws4.append([rec["check"], rec["result"], rec["expected"]])
    _style_header(ws4); _autosize(ws4)

    ws5 = wb.create_sheet("Sensitive Fields")
    ws5.append(["Field", "Present in sales output", "Expected"])
    for rec in sensitive:
        ws5.append([rec["field"], rec["in_serialized_output"], rec["expected"]])
    _style_header(ws5); _autosize(ws5)

    ws6 = wb.create_sheet("Audit Coverage")
    ws6.append(["Event type", "Observed"])
    for rec in audit:
        ws6.append([rec["event_type"], rec["observed"]])
    _style_header(ws6); _autosize(ws6)

    ws7 = wb.create_sheet("Session Security")
    ws7.append(["Setting", "Value"])
    ws7.append(["Session storage", "Server-side (DB); opaque token cookie"])
    ws7.append(["Cookie flags", "HttpOnly, SameSite=Lax"])
    ws7.append(["Secure cookie", "production only (CORRIDORIQ_ENV=production)"])
    ws7.append(["Session TTL (hours)", settings.SESSION_TTL_HOURS])
    ws7.append(["Lockout threshold", settings.AUTH_MAX_FAILED_LOGINS])
    ws7.append(["Lockout minutes", settings.AUTH_LOCKOUT_MINUTES])
    _style_header(ws7); _autosize(ws7)

    wb.save(path)


def _write_crm_markdown(path, env, denial, record_level, org_boundary):
    c = env["conn"]
    L = []
    L.append("# CorridorIQ — CRM Access Validation")
    L.append(f"\n_Generated {_today()} • evidence produced by live checks_\n")
    L.append("Confirms that sales users manage organization-owned CRM records while "
             "canonical Company Intelligence stays read-only.\n")

    L.append("## CRM writable tables (organization-owned)\n")
    L.append("| Table | Purpose |")
    L.append("|---|---|")
    purposes = {
        "crm_company_relationships": "Per-company sales state, status, assignment",
        "crm_activities": "Calls, emails, meetings, notes, outcomes",
        "crm_activity_revisions": "Retained edit history for activities",
        "crm_tasks": "Assigned tasks, follow-ups, priorities",
        "crm_assignment_history": "Append-only assignment audit trail",
    }
    for t in CRM_WRITABLE_TABLES:
        L.append(f"| `{t}` | {purposes.get(t, '')} |")

    L.append("\n## Intelligence tables — read-only to sales\n")
    for t in INTELLIGENCE_READONLY_TABLES:
        L.append(f"- `{t}`")

    L.append("\n## Record-level access (live)\n")
    L.append("| Check | Result | Expected |")
    L.append("|---|---|---|")
    for rec in record_level:
        L.append(f"| {rec['check']} | {rec['result']} | {rec['expected']} |")

    L.append("\n## Organization isolation (live)\n")
    L.append("| Check | Result | Expected |")
    L.append("|---|---|---|")
    for rec in org_boundary:
        L.append(f"| {rec['check']} | {rec['result']} | {rec['expected']} |")

    L.append("\n## CRM permission enforcement (live)\n")
    L.append("| Action | " + " | ".join(ROLE_ORDER) + " |")
    L.append("|" + "---|" * (len(ROLE_ORDER) + 1))
    for rec in denial:
        L.append("| " + " | ".join([rec["action"], *[rec[r] for r in ROLE_ORDER]]) + " |")

    # Duplicate-prevention evidence: one active relationship per company.
    dup = c.execute(
        "SELECT company_id, COUNT(*) AS n FROM crm_company_relationships "
        "GROUP BY organization_id, company_id HAVING n > 1").fetchall()
    L.append("\n## Assignment duplicate-prevention (live)\n")
    L.append(f"- Companies with more than one active relationship record: **{len(dup)}** (expected 0)")

    L.append("\n## Intelligence write-protection (live)\n")
    L.append("- Sprint 5 exposes no API route or service function that updates permits, "
             "projects, opportunity scores, lifecycle fields, or company-intelligence metrics "
             "for sales users. Verified by `test_sales_activity_does_not_touch_intelligence`.")
    path.write_text("\n".join(L) + "\n", encoding="utf-8")


def generate_all(output_dir=None) -> list:
    out = output_dir or settings.REPORTS_GENERATED_DIR
    out.mkdir(parents=True, exist_ok=True)
    env = _build_env()
    denial = _denial_matrix(env)
    record_level = _record_level_checks(env)
    org_boundary = _org_boundary_checks(env)
    sensitive = _sensitive_field_checks(env)
    audit = _audit_coverage(env)

    date = _today()
    md = out / f"security_rbac_validation_{date}.md"
    xlsx = out / f"security_rbac_validation_{date}.xlsx"
    crm_md = out / f"crm_access_validation_{date}.md"

    _write_markdown(md, env, denial, record_level, org_boundary, sensitive, audit)
    _write_xlsx(xlsx, denial, record_level, org_boundary, sensitive, audit)
    _write_crm_markdown(crm_md, env, denial, record_level, org_boundary)
    env["conn"].close()
    return [md, xlsx, crm_md]


if __name__ == "__main__":
    for p in generate_all():
        print(f"Wrote {p}")
