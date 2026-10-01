/* Executive sales dashboard — answers "what requires attention today?" */
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

  // The map workspace (map + feed) uses the assigned context; its View all
  // opens the same context and scope. The stat strip uses the same feed.
  const OPP_CONTEXT = "assigned";
  const OPP_HREF = "opportunities.html?context=" + OPP_CONTEXT;

  function renderFeed(companies) {
    const k = (data && data.kpis) || {};
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

  function companyCard(c) {
    const overdue = c.followup_overdue
      ? `<span class="badge red">Overdue</span>` : "";
    return `<div class="company-card">
      <div class="cc-top">
        <div>
          <div class="cc-name"><a href="sales-company-profile.html?id=${c.company_id}">${CIQ.esc(c.display_name)}</a></div>
          <div class="cc-loc">${CIQ.esc([c.city, c.state].filter(Boolean).join(", ") || "—")}</div>
        </div>
        <div class="stack" style="align-items:flex-end;gap:6px">
          ${CIQ.relevanceBadge(c.account_relevance)}
          ${CIQ.statusBadge(c.relationship_status)}
        </div>
      </div>
      <div class="cc-reason">${CIQ.esc(c.reason)}</div>
      <div class="cc-meta">
        <div><span>Active projects</span>${c.active_projects || 0}</div>
        <div><span>Top project</span>${CIQ.bandBadge(c.highest_opportunity_score)}</div>
        <div><span>Last contact</span>${c.last_contact_at ? CIQ.relTime(c.last_contact_at) : "Never"}</div>
        <div><span>Next follow-up</span>${c.next_followup_at ? CIQ.fmtDate(c.next_followup_at) : "—"} ${overdue}</div>
      </div>
      <div class="cc-action"><span>Next:</span> <span class="next">${CIQ.esc(c.recommended_action)}</span></div>
      <div class="cc-quick">
        <button class="btn btn-sm" data-a="call" data-id="${c.company_id}" data-name="${CIQ.esc(c.display_name)}">Call</button>
        <button class="btn btn-sm" data-a="log" data-id="${c.company_id}" data-name="${CIQ.esc(c.display_name)}">Log activity</button>
        <button class="btn btn-sm" data-a="followup" data-id="${c.company_id}" data-name="${CIQ.esc(c.display_name)}">Follow-up</button>
        <a class="btn btn-sm btn-ghost" href="sales-company-profile.html?id=${c.company_id}">Open</a>
      </div>
    </div>`;
  }

  function renderPriority() {
    const el = document.getElementById("priority");
    const items = data.priority_companies || [];
    if (!items.length) {
      const assignmentScoped = data.assignment_scoped !== false
        && !(CIQ.user && CIQ.user.dashboard_mode === "organization");
      if (assignmentScoped) {
        el.innerHTML = CIQ.emptyState({ title: "No companies assigned to you",
          text: "When companies are assigned to you they'll appear here, prioritized by opportunity." });
      } else {
        el.innerHTML = CIQ.emptyState({ title: "No priority companies right now",
          text: "Organization relationships will appear here when CRM assignments exist." });
      }
      return;
    }
    el.className = "grid-cards";
    el.innerHTML = items.map(companyCard).join("");
    el.querySelectorAll("button[data-a]").forEach((b) => b.addEventListener("click", () => {
      const id = Number(b.dataset.id), name = b.dataset.name;
      const prefill = b.dataset.a === "call" ? { activity_type: "call" }
        : b.dataset.a === "followup" ? { activity_type: "follow_up" } : {};
      CIQ.logActivity({ companyId: id, companyName: name, prefill, onSaved: load });
    }));
  }

  function renderFollowups() {
    const el = document.getElementById("followups");
    const items = data.followups_due || [];
    if (!items.length) {
      el.innerHTML = CIQ.emptyState({ compact: true, icon: "✓", title: "No follow-ups due", text: "You're all caught up." });
      return;
    }
    el.innerHTML = `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
      <th>Company</th><th>Owner</th><th>Due</th><th>Last outcome</th><th>Required action</th><th></th>
      </tr></thead><tbody>${items.map((f) => `<tr>
        <td data-label="Company"><a href="sales-company-profile.html?id=${f.company_id}">${CIQ.esc(f.display_name)}</a></td>
        <td data-label="Owner">${CIQ.esc(f.assigned_to || "—")}</td>
        <td data-label="Due">${CIQ.fmtDate(f.next_followup_at)} ${f.overdue ? '<span class="badge red">Overdue</span>' : ""}</td>
        <td data-label="Last outcome">${f.last_outcome ? CIQ.esc(CIQ.outcomeLabel(f.last_outcome)) : '<span class="muted">—</span>'}</td>
        <td data-label="Action">${CIQ.esc(f.recommended_action)}</td>
        <td data-label=""><button class="btn btn-sm" data-id="${f.company_id}" data-name="${CIQ.esc(f.display_name)}">Log</button></td>
      </tr>`).join("")}</tbody></table></div>`;
    el.querySelectorAll("button[data-id]").forEach((b) => b.addEventListener("click", () =>
      CIQ.logActivity({ companyId: Number(b.dataset.id), companyName: b.dataset.name, onSaved: load })));
  }

  const REFRESH_CLASS = { succeeded: "green", partial: "amber", failed: "red", running: "" };

  async function renderRefresh() {
    const el = document.getElementById("refreshCard");
    let simple;
    try { simple = await CIQ.api.get("/api/status/refresh"); }
    catch (e) { el.innerHTML = ""; return; }
    const status = simple.status || "none";
    const cls = REFRESH_CLASS[status] || "";
    const when = simple.last_completed ? CIQ.relTime(simple.last_completed) : "—";
    const canRun = CIQ.hasPerm("pipeline.run");
    el.innerHTML = `<div class="dash-status">
      <span>Last refresh ${CIQ.esc(when)}</span>
      <span class="badge ${cls}">${CIQ.esc(simple.label || "—")}</span>
      ${canRun ? `<button class="btn btn-sm" id="runRefreshBtn" type="button">Run refresh</button>` : ""}
    </div>`;
    const btn = document.getElementById("runRefreshBtn");
    if (btn) btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await CIQ.api.post("/api/admin/morning-refresh/run", {});
        CIQ.toast("Morning refresh started.", "info");
        setTimeout(renderRefresh, 2500);
      } catch (e) {
        CIQ.toast(e.message || "Could not start refresh.", "error");
        btn.disabled = false;
      }
    });
  }

  function renderSummary() {
    const s = data.activity_summary || {};
    const defs = [
      ["calls_logged", "Calls logged"], ["conversations", "Conversations"],
      ["appointments", "Appointments"], ["quote_requests", "Quote requests"],
      ["followups_completed", "Follow-ups done"], ["companies_contacted", "Companies contacted"],
    ];
    document.getElementById("summary").innerHTML = defs.map(([k, l]) =>
      `<div class="kpi" style="cursor:default"><div class="kpi-val">${s[k] || 0}</div>
       <div class="kpi-label">${l}</div></div>`).join("");
  }

  async function load() {
    try {
      data = await CIQ.api.get("/api/sales/dashboard");
    } catch (e) {
      CIQ.content.innerHTML = CIQ.errorBanner(e.message || "Could not load dashboard");
      return;
    }
    CIQ.setTaskCount((data.kpis && data.kpis.tasks_due_today) || 0);

    let companies = [];
    try {
      const listed = await CIQ.api.get("/api/sales/companies?page_size=50");
      companies = listed.items || [];
    } catch (e) { companies = data.priority_companies || []; }

    renderFeed(companies);
    CIQ.renderTodaysAccounts(document.getElementById("todaysAccounts"), data.todays_accounts);
    renderPriority(); renderFollowups(); renderSummary();
    CIQ.renderRfqEntry(document.getElementById("rfqEntry"));
    renderRefresh();
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard(null, { title: "Dashboard", subtitle: "What needs attention today", active: "sales-dashboard.html" });
    if (!user) return;
    // Admins / managers belong on their role dashboards, not the rep view.
    if (user.dashboard_mode === "organization" || user.dashboard_mode === "team"
        || user.dashboard_mode === "estimator" || user.dashboard_mode === "read_only") {
      const dest = user.default_landing_page || CIQ.landingPage();
      if (dest && !location.pathname.endsWith(dest)) {
        location.replace(dest);
        return;
      }
    }
    CIQ.renderHero();
    document.getElementById("execStrip").innerHTML = CIQ.skeletonRows(1);
    document.getElementById("priority").innerHTML = CIQ.skeletonRows(2);
    load();
  });
})();
