/* My Companies — scannable cards with filters, saved views, pagination. */
(function () {
  let isManager = false;
  let page = 1;
  const filters = {};

  const VIEWS = [
    ["priority", "Priority", {}],
    ["never", "Never Contacted", { contacted: "never" }],
    ["followup", "Follow-Up Due", { followup: "due" }],
    ["new", "New Opportunities", { status: "assigned" }],
    ["quote", "Quote Requested", { status: "quote_requested" }],
    ["won", "Won", { status: "won" }],
    ["lost", "Lost", { status: "lost" }],
    ["dnc", "Do Not Contact", { do_not_contact: "1" }],
  ];

  function renderViews(active) {
    document.getElementById("views").innerHTML = VIEWS.map(([key, label]) =>
      `<button class="chip ${active === key ? "active" : ""}" data-view="${key}">${label}</button>`).join("");
    document.querySelectorAll("#views .chip").forEach((c) => c.addEventListener("click", () => applyView(c.dataset.view)));
  }

  function applyView(key) {
    const def = VIEWS.find((v) => v[0] === key);
    // Reset filter-driven fields, keep search text.
    ["status", "tier", "followup", "contacted", "do_not_contact", "municipality"].forEach((k) => delete filters[k]);
    document.getElementById("fstatus").value = "";
    document.getElementById("ftier").value = "";
    document.getElementById("ffollowup").value = "";
    document.getElementById("fcontacted").value = "";
    document.getElementById("fmuni").value = "";
    Object.assign(filters, def ? def[2] : {});
    document.getElementById("ffollowup").value = filters.followup || "";
    document.getElementById("fstatus").value = filters.status || "";
    document.getElementById("fcontacted").value = filters.contacted || "";
    renderViews(key);
    page = 1; load();
  }

  function card(c) {
    const overdue = c.followup_overdue ? `<span class="badge red">Overdue</span>` : "";
    const repRow = isManager ? `<div><span>Assigned to</span>${CIQ.esc(c.assigned_to || "Unassigned")}</div>` : "";
    return `<div class="company-card">
      <div class="cc-top">
        <div>
          <div class="cc-name"><a href="sales-company-profile.html?id=${c.company_id}">${CIQ.esc(c.display_name)}</a></div>
          <div class="cc-loc">${CIQ.esc([c.city, c.state].filter(Boolean).join(", ") || "—")}${c.primary_role ? " · " + CIQ.esc(CIQ.titleCase(c.primary_role)) : ""}</div>
        </div>
        <div class="stack" style="align-items:flex-end;gap:6px">
          ${CIQ.relevanceBadge(c.account_relevance)}${CIQ.statusBadge(c.relationship_status)}
        </div>
      </div>
      <div class="cc-reason">${CIQ.esc(c.reason)}</div>
      <div class="cc-meta">
        <div><span>Active projects</span>${c.active_projects || 0}</div>
        <div><span>Last 30 days</span>${c.projects_last_30_days || 0}</div>
        <div><span>Last contact</span>${c.last_contact_at ? CIQ.relTime(c.last_contact_at) : "Never"}</div>
        <div><span>Next follow-up</span>${c.next_followup_at ? CIQ.fmtDate(c.next_followup_at) : "—"} ${overdue}</div>
        ${repRow}
        <div><span>Top score</span>${c.highest_opportunity_score != null ? Math.round(c.highest_opportunity_score) : "—"}</div>
      </div>
      <div class="cc-action"><span>Next:</span> <span class="next">${CIQ.esc(c.recommended_action)}</span></div>
      <div class="cc-quick">
        <button class="btn btn-sm" data-a="log" data-id="${c.company_id}" data-name="${CIQ.esc(c.display_name)}">Log activity</button>
        <a class="btn btn-sm btn-ghost" href="sales-company-profile.html?id=${c.company_id}">Open</a>
      </div>
    </div>`;
  }

  async function load() {
    const list = document.getElementById("list");
    list.className = ""; list.innerHTML = CIQ.skeletonRows(4);
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([k, v]) => { if (v) params.set(k, v); });
    params.set("page", page); params.set("page_size", 24);
    let data;
    try { data = await CIQ.api.get("/api/sales/companies?" + params.toString()); }
    catch (e) { list.innerHTML = CIQ.errorBanner(e.message); return; }
    if (!data.items.length) {
      list.innerHTML = CIQ.emptyState({ icon: "⌂", title: "No companies match",
        text: "Try clearing filters or choosing a different saved view." });
      document.getElementById("pager").innerHTML = "";
      return;
    }
    list.className = "grid-cards";
    list.innerHTML = data.items.map(card).join("");
    list.querySelectorAll('button[data-a="log"]').forEach((b) => b.addEventListener("click", () =>
      CIQ.logActivity({ companyId: Number(b.dataset.id), companyName: b.dataset.name, onSaved: load })));
    CIQ.pager(document.getElementById("pager"), {
      page: data.page, pages: data.pages, total: data.total,
      onPage: (p) => { page = p; load(); window.scrollTo(0, 0); },
    });
  }

  function wireFilters() {
    const set = (id, key) => {
      const el = document.getElementById(id);
      el.addEventListener("change", () => { filters[key] = el.value; page = 1; renderViews(null); load(); });
    };
    const fq = document.getElementById("fq");
    fq.addEventListener("input", CIQ.debounce(() => { filters.q = fq.value.trim(); page = 1; load(); }, 350));
    set("fstatus", "status"); set("ftier", "tier"); set("ffollowup", "followup");
    set("fcontacted", "contacted");
    const fmuni = document.getElementById("fmuni");
    fmuni.addEventListener("input", CIQ.debounce(() => { filters.municipality = fmuni.value.trim(); page = 1; load(); }, 350));
    if (isManager) {
      const frep = document.getElementById("frep");
      frep.addEventListener("change", () => { filters.assigned_user_id = frep.value; page = 1; load(); });
    }
    document.getElementById("fclear").addEventListener("click", () => {
      Object.keys(filters).forEach((k) => delete filters[k]);
      ["fq", "fstatus", "ftier", "fmuni", "ffollowup", "fcontacted", "frep"].forEach((id) => {
        const el = document.getElementById(id); if (el) el.value = "";
      });
      page = 1; renderViews("priority"); load();
    });
  }

  async function populateStatuses() {
    const sel = document.getElementById("fstatus");
    [["new", "New"], ["assigned", "Assigned"], ["contacted", "Contacted"],
     ["qualified", "Qualified"], ["follow_up", "Follow-Up"], ["quote_requested", "Quote Requested"],
     ["quote_sent", "Quote Sent"], ["negotiating", "Negotiating"], ["won", "Won"],
     ["lost", "Lost"], ["do_not_contact", "Do Not Contact"]].forEach(([v, l]) => {
      const o = document.createElement("option"); o.value = v; o.textContent = l; sel.appendChild(o);
    });
  }

  async function populateReps() {
    if (!isManager) return;
    try {
      const t = await CIQ.api.get("/api/manager/team");
      const frep = document.getElementById("frep");
      frep.style.display = "";
      t.items.forEach((u) => {
        const o = document.createElement("option"); o.value = u.user_id; o.textContent = u.display_name; frep.appendChild(o);
      });
    } catch (e) { /* not a manager */ }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("crm.relationships.view",
      { title: "My Companies", subtitle: "Assigned accounts, prioritized", active: "my-companies.html" });
    if (!user) return;
    isManager = CIQ.hasPerm("companies.view");
    populateStatuses(); wireFilters(); await populateReps();

    // Deep-link params from KPIs / search.
    const q = CIQ.qs("q"), status = CIQ.qs("status"), tier = CIQ.qs("tier"), view = CIQ.qs("view");
    if (q) { filters.q = q; document.getElementById("fq").value = q; }
    if (status) { filters.status = status; document.getElementById("fstatus").value = status; }
    if (tier) { filters.tier = tier; document.getElementById("ftier").value = tier; }
    if (view === "followup") { filters.followup = "due"; document.getElementById("ffollowup").value = "due"; renderViews("followup"); }
    else if (view === "overdue") { filters.followup = "overdue"; document.getElementById("ffollowup").value = "overdue"; renderViews(null); }
    else if (!q && !status && !tier) { renderViews("priority"); }
    else renderViews(null);
    load();
  });
})();
