# Outbound readiness audit: stale data and CEO Agent access

Audited commit: `fcf50fe` (`release/corridoriq-2026-10-06`). Audit date: 2026-10-06.
Scope: admin dashboard, sales dashboard, Opportunities, Opportunity Map, company
records, permit/project recency, Today's accounts, refresh status, reports, CEO
Morning Brief, and generated artifacts. Nothing was deleted, and no production
data, deployment, Railway setting or secret was touched.

## What was already sound

- **Today's accounts** (`pipeline/trust/opportunities.py`, `gates.evaluate`,
  `account_view.todays_accounts`) is fail-closed. A card needs a FRESH or LAGGING
  city feed, a refresh that succeeded within 7 days, activity within 30 days,
  verified identity, a verified phone or email on that company's own row, and no
  negative human outcome. The queue is never padded. Account priority only orders
  cards that have already passed every gate.
- Opportunity dates are source dates: `analysis.scoring.compute_opportunity_date`
  takes the filed date, then the issued date, then the finaled date. None is invented.
- The CEO brief is published only after a refresh succeeds. A failed or malformed
  brief is never shown as new.
- The procurement preview (`rfq-inbox.html`, `contractor-*.html`) is limited to
  admins, says "Sample data" in a banner and a pill, sends nothing, and is kept
  separate from the real `/api/pilot/...` flow.

## Confirmed stale-data risks

| # | Risk | Severity | Where | Status |
|---|------|----------|-------|--------|
| 1 | The **assigned** Opportunities view had no date window. A years-old permit with a high score ranked first on Opportunities, the dashboard's Top Opportunities, and the map. | **High** | `crm/service.py::opportunities`, `opportunity_map` | Fixed |
| 2 | The refresh badge said **"Data current"** for any succeeded latest run, however old. If the scheduler stopped, every dashboard still showed green. | **High** | `pipeline_runs.employee_status` (used by the sales, read-only and admin dashboards) | Fixed |
| 3 | Admin "new submitted" and "new issued" KPIs fell back to the **latest run summary of any age** whenever today's live count was 0. Old "new" counts appeared as today's. | **High** | `crm/admin.py::admin_dashboard` | Fixed |
| 4 | "Recent opportunity activity" lists had **no window and no date shown** (read-only dashboard) or were **sorted by ingestion time** (admin, `COALESCE(opportunity_date, last_updated_at)`). An undated record touched today looked newest. | Medium-High | `crm/service.py::recent_opportunity_activity`, `crm/admin.py::admin_dashboard`, `readonly-dashboard.js` | Fixed |
| 5 | Stale or disconnected city feeds still produced Opportunities and map rows with **no warning**. Only Today's accounts checked the feed. | Medium-High | `crm/service.py::opportunities` | Fixed (labelled) |
| 6 | Account priority was presented as intent. The sales and read-only "High priority" KPI counted plumbing-fit accounts. The admin KPI with the same name counted trust-gated accounts to act on today. The CEO brief's "TOP OPPORTUNITIES" listed stored account priority with no recency. | Medium | `crm/service.py::dashboard`, `agents/ceo/analytics/brief.py` | Fixed |
| 7 | Admin jurisdiction freshness fell back to **"Current" when the sync status was success or NULL**. This happened whenever the latest run had no summary: no run yet, a run in progress, or a crashed run. The empty state read "All jurisdictions are current". | Medium | `crm/admin.py::admin_dashboard`, `admin-dashboard.js` | Fixed |
| 8 | "Issued today" counted permits with **no issued date** if `last_updated_at` was today, so an ingestion time stood in for an issue date. | Medium | `crm/admin.py::admin_dashboard` | Fixed |
| 9 | A CEO brief matching the latest refresh was **"current" regardless of that refresh's age**. A stored "ALL SYSTEMS HEALTHY" record also stayed green indefinitely, because health is only re-checked during a refresh. | Medium | `agents/ceo/analytics/serve.py::_currency`, `resolve_health` | Fixed |
| 10 | Generated reports survive refreshes and showed only a file timestamp. Nothing indicated that a report predated the current data. | Low-Medium | `pipeline/reports/catalog.py`, `reports.js` | Fixed (labelled) |
| 11 | Future-dated permits (source errors) counted as "recent" in the organization view. | Low | `crm/service.py::opportunities` | Fixed |
| 12 | The CEO notice for a running refresh read "Latest refresh None finished as running". | Low | `serve.py::_currency` | Fixed |

## What changed

One rule set now covers every feed (`pipeline/trust/recency.py`):

- **Activity date:** the source issued date, else the source filed date, else the
  stored opportunity date. It is never an ingestion timestamp.
- **Recent window:** 60 days (`gates.REVIEW_WINDOW_DAYS`), and never later than
  today. Rows older than 30 days (`CALL_WINDOW_DAYS`) are labelled as outside the
  call window.
- **Source freshness:** the trust-layer reading (`trust.health.collect_health`)
  that already gates Today's accounts. Rows from a STALE, NO_DATA, or disconnected
  feed are labelled.

| Surface | Change |
|---|---|
| Opportunities and map (both contexts) | Both contexts use the same 60-day window. The assigned view can show older projects only through an explicit **"Include older history"** option (`recency=all`), and those rows are labelled `HISTORICAL`. Every row carries `activity_date`, `days_since_activity`, `recency`, `source_freshness`, `source_stale` and `freshness_note`. The cache key now includes the day. |
| Recent activity lists (sales, read-only, admin) | The same window applies. Rows are sorted by source activity date and show their age and freshness badge. |
| Refresh badge | "Data current" requires a **succeeded** refresh within **36 h**, the same threshold the trust layer and CEO health use. Otherwise the badge reads "Data may be out of date" in amber. A newer failed, partial or running run never makes data current. A running refresh shows the previous completion time. |
| Admin KPIs | The run-summary fallback applies only when that run completed today. "Issued today" needs a source issued date. |
| Admin freshness panel | With no summary, freshness is computed from data using the trust-layer rule ("No data" or "Stale" instead of "Current"). Every row is cross-checked with the trust layer and flagged **Outreach blocked** when the daily queue excludes that city. The empty state no longer claims feeds are current. |
| Sales and read-only "High priority" | Now equals accounts to act on today, the same as the admin dashboard. Account fit moved to `core_relevance_accounts`. |
| CEO brief | The brief is "current" only if its refresh finished within 36 h. A stale stored health record that said healthy is served as `unknown`, with a notice. "TOP OPPORTUNITIES" now says the ranking is account fit, not buying intent, and gives each account's last relevant activity date. |
| Reports | Files generated before the latest successful refresh are flagged "Older than latest data". They are not deleted or hidden. |
| Procurement preview | Buttons now read "Submit sample quote", "Send sample RFQ" and "Select supplier (sample)". |

### CEO Agent authorization (owner only)

- New permission `owner.ceo_agent` belongs to `OWNER_ONLY_PERMISSIONS`. New role
  `owner` (Business Owner) grants that permission and nothing else, and is meant
  to be combined with `admin`.
- `admin.system` **does not** imply it. The `admin` role excludes it,
  `load_user_permissions` never expands it, `has_permission` requires an exact
  grant, and the browser's `CIQ.hasPerm` treats `owner.*` the same way.
  `admin.system` is otherwise unchanged; admins keep every other permission.
- `GET /api/admin/ceo-morning-brief` and the page files (`ceo-morning-brief.html`
  and `.js`) are gated on the server by `rbac.require_ceo_owner`: the exact grant
  plus membership of CorridorIQ's own organization. Unauthenticated requests are
  redirected to login. Every other signed-in role gets 403 and an audit entry.
  The page gate is decided on the resolved file, so percent-encoded or re-cased
  URLs are refused too.
- The admin API cannot grant or remove the `owner` role. Self-escalation through
  `PATCH /api/admin/users/<id>` is refused. A non-owner admin cannot edit, disable
  or re-email the owner's account, which closes the take-over route of changing
  the email and then resetting the password. Role edits made through the API keep
  an existing owner grant, so they cannot lock the owner out.
- Grants and revocations happen only through the audited server command
  `python -m pipeline.auth.grant_owner`. No email address is hard-coded anywhere.

## Required migration step (not performed)

After this release is deployed, the owner has no `owner` role, so the CEO Morning
Brief returns 403 and its menu item is hidden until the role is granted. Nothing
else changes for the owner, who keeps full admin access, and briefs keep being
published. Granting the role writes to production data, so it was **not** done
here. Run this in a shell on the **production service**, against the production
database (for example `railway ssh` into the web service, from the app directory):

```
python -m pipeline.auth.grant_owner --email <owner email> --dry-run
python -m pipeline.auth.grant_owner --email <owner email>
```

The target must be an active `admin` in the `corridoriq` organization. The
command is idempotent and audited (`grant_owner_role`). The grant takes effect on
the next request, without logging in again. To undo it, add `--revoke`.

## Intentionally left unchanged

| Item | Why |
|---|---|
| Contact verification age. A contact verified years ago still counts as verified for Today's accounts. | Needs a re-verification policy, such as N days per channel type. Medium risk. |
| `pipeline_runs._compute_counts` "new issued permits" counts touched permits in the issued stage, including old permits whose fields changed. | Changing it changes pipeline semantics. The admin dashboard now uses it only for a run that completed the same day. Low-Medium. |
| The refresh summary's per-jurisdiction freshness uses the newest record fetched in that run, while the trust layer uses the newest record in the database. These are two different rules. | Not unified. The admin panel now shows both readings and flags **Outreach blocked** from the trust layer. |
| A refresh between 36 h and 7 days old ("AGING") still allows Call/Email in Today's accounts at Medium confidence, with a visible note. | Existing, deliberate policy. The refresh badge now turns amber after 36 h. |
| Chronic **partial** runs. Only `succeeded` counts as a refresh, so if one city fails every day, Today's accounts fail closed (go empty) after 7 days and the CEO brief stays "older". | This is the correct fail-closed behaviour, but it is an operational trap. See next actions. |
| Priority company cards. The "Top project" band uses the all-time highest score, and "Make first contact" is recommended for any account never contacted, whatever its recent activity. | Needs a product decision. Account relevance still orders the list correctly. |
| `company_intelligence` values such as "N new projects in 30d" are computed at refresh time, not as of today. | Low. They become stale only when refreshes stop, which the refresh badge now shows. |
| The reports catalog lists every generated file, including security and UI audit files, to any role with `reports.view`. | Not a stale-data issue. Flagged for follow-up below. |
| The procurement preview remains a preview. | As instructed. Only the labels changed. |

## Recommended next actions

1. **Before outreach:** run the owner grant above, confirm the CEO page loads for
   the owner, and confirm it returns 403 for a test admin.
2. Check that the latest run in `/api/admin/morning-refresh` is `succeeded`, not
   `partial`. If any city fails daily, fix it or decide whether `partial` should
   count as a refresh.
3. Set a contact re-verification window and enforce it in
   `trust.gates.contact_state` / `trust.contacts`.
4. Restrict the reports catalog by category and role. Sales reps should not
   download security or UI audit files.
5. Have `pipeline_runs._compute_counts` count only permits whose source issued or
   filed date falls on the run day.
6. Gate "Make first contact" on recent activity, using the same recency rule.
7. Pin Python 3.14 for deploy and CI. `pipeline/analysis/scoring.py` depends on
   deferred annotation evaluation and fails to import on 3.12.
