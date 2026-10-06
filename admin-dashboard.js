/* Organization-wide administrator dashboard — never assignment-scoped. */
(function () {
  let data = null;

  function companyMetaMap(companies) {
    const map = {};
    (companies || []).forEach((c) => {
      map[c.company_id] = {
        primary_role: c.primary_role,
        full_name: c.contact_name || c.full_name,
        job_title: c.job_title,
        phone: c.main_phone || c.phone,
      };
    });
    return map;
  }

  // The map workspace (map + feed) uses the organization context; its View all
  // opens the same context and scope. The stat strip uses the same feed.
  const OPP_CONTEXT = "organization";
  const OPP_HREF = "opportunities.html?context=" + OPP_CONTEXT;

  async function renderFeed() {
    const k = (data && data.kpis) || {};
    let companies = [];
    try {
      const listed = await CIQ.api.get("/api/sales/companies?page_size=50");
      companies = listed.items || [];
    } catch (e) { companies = []; }
    const companyMeta = companyMetaMap(companies);
    CIQ.mapWorkspace(document.getElementById("mapWorkspace"), {
      context: OPP_CONTEXT,
      todaysAccounts: (data.todays_accounts && data.todays_accounts.items) || [],
      onLoaded: (feed) => {
        const items = feed.items || [];
        const stats = CIQ.summarizeOpportunities({ items, kpis: k, companyMeta, total: feed.total });
        CIQ.renderExecStrip(document.getElementById("execStrip"), stats, { listHref: OPP_HREF });
      },
    });
  }

  const ACTIONS = [
    ["opportunities.html", "Review opportunities"],
    ["my-companies.html", "View companies"],
    ["assignments.html", "Assign work"],
    ["estimator-work-queue.html", "Review estimates"],
    ["user-management.html", "Manage users"],
    ["experience-preview.html", "Experience preview"],
    ["catalog-admin.html", "Administration"],
  ];

  function renderActions() {
    document.getElementById("actions").innerHTML = ACTIONS.map(([href, label]) => {
      if (href === "#run-refresh") {
        return `<button class="btn btn-sm" type="button" data-a="refresh">${CIQ.esc(label)}</button>`;
      }
      return `<a class="btn btn-sm" href="${href}">${CIQ.esc(label)}</a>`;
    }).join("");
    document.querySelectorAll('[data-a="refresh"]').forEach((b) =>
      b.addEventListener("click", runMorningRefresh));
  }

  async function runMorningRefresh() {
    if (!CIQ.hasPerm("pipeline.run")) {
      CIQ.toast("You do not have permission to run the morning refresh.", "error");
      return;
    }
    try {
      await CIQ.api.post("/api/admin/morning-refresh/run", {});
      CIQ.toast("Morning refresh started", "success");
      setTimeout(load, 1200);
    } catch (e) {
      CIQ.toast(e.message || "Could not start refresh", "error");
    }
  }

  // Amber when the newest refresh "succeeded" but is too old to call current.
  function refreshClass(r) {
    if (r.stale && r.status === "succeeded") return "amber";
    return ({ succeeded: "green", partial: "amber", failed: "red", running: "" })[r.status] || "";
  }

  function renderRefresh() {
    const el = document.getElementById("refreshCard");
    const mr = data.morning_refresh || {};
    const cls = refreshClass(mr);
    const when = mr.last_completed ? CIQ.relTime(mr.last_completed) : "—";
    const running = mr.running ? " · in progress" : "";
    el.innerHTML = `<div class="dash-status">
      <span>Last refresh ${when}${running}</span>
      <span class="badge ${cls}">${CIQ.esc(mr.label || "No refresh yet")}</span>
    </div>`;
  }

  function renderFreshness() {
    const el = document.getElementById("freshness");
    const items = data.jurisdiction_freshness || [];
    if (!items.length) {
      el.innerHTML = CIQ.emptyState({ title: "Freshness not available yet",
        text: "Jurisdiction freshness will appear after the next morning refresh. Do not assume feeds are current." });
      return;
    }
    // A feed the trust layer blocks from Today's accounts is not "current" here either.
    const bad = items.filter((j) => (j.status && j.status !== "Current") || j.outreach_blocked);
    if (!bad.length) {
      el.innerHTML = CIQ.emptyState({ icon: "✓", title: "All jurisdictions are current",
        text: `${items.length} jurisdiction${items.length === 1 ? "" : "s"} reporting.` });
    }
    const rows = (bad.length ? bad : items).map((j) => {
      const cls = j.status === "Current" ? "green" : j.status === "Delayed" ? "amber" : "red";
      return `<tr><td data-label="Jurisdiction">${CIQ.esc(j.name || j.slug || "—")}</td>
        <td data-label="Status"><span class="badge ${cls}">${CIQ.esc(j.status || "—")}</span>${j.outreach_blocked ? ' <span class="badge red" title="Excluded from the daily call list until the feed is current">Outreach blocked</span>' : ""}</td>
        <td data-label="Synced">${j.newest_source_date ? CIQ.fmtDate(j.newest_source_date) : "—"}</td>
        <td data-label="Today">${j.records_received_today || 0}</td></tr>`;
    }).join("");
    el.innerHTML = (bad.length ? "" : el.innerHTML) +
      `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
      <th>Jurisdiction</th><th>Status</th><th>Newest source</th><th>Received today</th>
      </tr></thead><tbody>${rows}</tbody></table></div>`;
  }

  function renderPipeline() {
    const el = document.getElementById("pipeline");
    const items = data.recent_pipeline_activity || [];
    if (!items.length) {
      el.innerHTML = CIQ.emptyState({ title: "No pipeline runs yet",
        text: "Run a morning refresh to populate pipeline history." });
      return;
    }
    el.innerHTML = `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
      <th>Started</th><th>Status</th><th>Source</th><th>Succeeded</th><th>Failed</th><th>Created</th><th>Updated</th>
      </tr></thead><tbody>${items.map((r) => `<tr>
        <td data-label="Started">${r.started_at ? CIQ.relTime(r.started_at) : "—"}</td>
        <td data-label="Status"><span class="badge">${CIQ.esc(r.status || "—")}</span></td>
        <td data-label="Source">${CIQ.esc(r.trigger_source || "—")}</td>
        <td data-label="Succeeded">${r.jurisdictions_succeeded ?? "—"}</td>
        <td data-label="Failed">${r.jurisdictions_failed ?? "—"}</td>
        <td data-label="Created">${r.records_created ?? "—"}</td>
        <td data-label="Updated">${r.records_updated ?? "—"}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  function renderOpps() {
    const el = document.getElementById("opps");
    const items = data.recent_opportunity_activity || [];
    if (!items.length) {
      el.innerHTML = CIQ.emptyState({ title: "No opportunity activity in the last 60 days",
        text: "Only projects with a source permit date in the last 60 days appear here." });
      return;
    }
    el.innerHTML = `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
      <th>Company</th><th>City</th><th>Stage</th><th>Priority</th><th>Last activity</th>
      </tr></thead><tbody>${items.map((o) => `<tr>
        <td data-label="Company">${o.company_id ? `<a href="sales-company-profile.html?id=${o.company_id}">${CIQ.esc(o.display_name || "—")}</a>` : (o.display_name ? CIQ.esc(o.display_name) : '<span class="muted">No contractor attributed</span>')}</td>
        <td data-label="City">${CIQ.esc(CIQ.placeName(o.jurisdiction) || "—")}</td>
        <td data-label="Stage">${CIQ.esc(CIQ.titleCase(o.project_lifecycle || "—"))}</td>
        <td data-label="Priority">${CIQ.bandBadge(o.opportunity_score)}</td>
        <td data-label="Last activity">${CIQ.activityAge(o)} ${CIQ.freshnessBadge(o)}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  async function load() {
    document.getElementById("execStrip").innerHTML = CIQ.skeletonRows
      ? CIQ.skeletonRows(1) : `<div class="muted">Loading…</div>`;
    // #mapWorkspace shows its own loading state; writing into it here would
    // wipe the live map on a reload.
    try {
      data = await CIQ.api.get("/api/admin/dashboard");
    } catch (e) {
      const msg = e.message || "Could not load admin dashboard";
      const hint = /unknown route|404/i.test(msg)
        ? " The CorridorIQ server needs a restart to load the admin dashboard API."
        : "";
      const el = document.getElementById("execStrip");
      if (el) el.innerHTML = CIQ.errorBanner(msg + hint);
      return;
    }
    renderRefresh();
    await renderFeed();
    CIQ.renderTodaysAccounts(document.getElementById("todaysAccounts"), data.todays_accounts);
    renderActions();
    CIQ.renderRfqEntry(document.getElementById("rfqEntry"));
    renderFreshness();
    renderPipeline();
    renderOpps();
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", {
      title: "Admin Dashboard",
      subtitle: "Organization-wide pipeline and CRM",
      active: "admin-dashboard.html",
    });
    if (!user) return;
    CIQ.renderHero();
    const btn = document.getElementById("runRefreshBtn");
    if (btn) {
      if (!CIQ.hasPerm("pipeline.run")) btn.style.display = "none";
      else btn.addEventListener("click", runMorningRefresh);
    }
    load();
  });
})();
