/* Opportunities — projects in an explicit context (assigned or organization),
   checked by the same trust layer the dashboards use. */
(function () {
  let page = 1; const filters = {}; let canEdit = false;

  const CONTEXT_TEXT = {
    assigned: "Projects of companies assigned to you (or with a CRM relationship, for managers)",
    organization: "Every project from the last 60 days, attributed or not",
  };

  function truncate(s, n) { s = s || ""; return s.length > n ? s.slice(0, n) + "…" : s; }

  function contactLine(p) {
    const c = p.contact;
    if (!c) return "";
    if (c.status !== "VERIFIED") return `<div class="muted" style="font-size:12.5px">${CIQ.esc(c.label || "Contact not yet verified.")}</div>`;
    const who = [c.name, c.title].filter(Boolean).join(", ") || "Business line";
    const link = c.phone ? `<a href="tel:${CIQ.esc(c.phone)}">${CIQ.esc(c.phone)}</a>`
      : c.email ? `<a href="mailto:${CIQ.esc(c.email)}">${CIQ.esc(c.email)}</a>` : "";
    return `<div style="font-size:12.5px"><span class="badge green">Verified</span> ${CIQ.esc(who)}${link ? " · " + link : ""}</div>`;
  }

  function card(p) {
    const company = p.company_id
      ? `<a href="sales-company-profile.html?id=${p.company_id}">${CIQ.esc(p.display_name || "Company")}</a>`
      : `<span class="muted">No contractor attributed</span>`;
    return `<div class="company-card">
      <div class="cc-top">
        <div><div class="cc-name" style="font-size:15px">${CIQ.esc(p.job_address || p.permit_number || "Project")}</div>
          <div class="cc-loc">${company} · ${CIQ.esc(CIQ.placeName(p.jurisdiction) || p.city || "")}</div></div>
        ${CIQ.bandBadge(p.opportunity_score)}
      </div>
      <div class="cc-meta">
        <div><span>Stage</span>${CIQ.esc(CIQ.titleCase(p.project_lifecycle || "—"))}</div>
        <div><span>Timing</span>${CIQ.esc(CIQ.titleCase(p.opportunity_timing || "—"))}</div>
        <div><span>Trade</span>${CIQ.esc(CIQ.titleCase(p.trade_scope || "—"))}</div>
        <div><span>Category</span>${CIQ.esc(CIQ.titleCase(p.project_category || "—"))}</div>
        <div><span>Opportunity</span>${p.opportunity_date ? CIQ.fmtDate(p.opportunity_date) : "—"}</div>
        <div><span>Related permits</span>${p.permit_count != null ? p.permit_count : "—"}</div>
        <div><span>Est. material value</span>${p.estimated_material_value != null ? CIQ.money(p.estimated_material_value) : "—"}</div>
      </div>
      ${p.account_relevance ? `<div>${CIQ.relevanceBadge(p.account_relevance)}</div>` : ""}
      ${contactLine(p)}
      ${p.description ? `<div class="cc-reason">${CIQ.esc(truncate(p.description, 120))}</div>` : ""}
      <div class="cc-quick">
        ${p.company_id ? `<a class="btn btn-sm btn-ghost" href="sales-company-profile.html?id=${p.company_id}">View company</a>` : ""}
        ${canEdit && p.company_id ? `<button class="btn btn-sm" data-cid="${p.company_id}" data-pid="${p.project_id}" data-addr="${CIQ.esc(p.job_address || "")}" data-name="${CIQ.esc(p.display_name)}">Log activity</button>` : ""}
      </div>
    </div>`;
  }

  function note(data) {
    const ctx = data.context || filters.context || "assigned";
    const bits = [CONTEXT_TEXT[ctx] || ctx];
    bits.push(data.include_all_scopes ? "all trades shown"
      : "projects outside your trade focus are hidden");
    if (!data.include_all_scopes && data.hidden_by_checks)
      bits.push(`${data.hidden_by_checks.toLocaleString("en-US")} not shown`);
    if (!data.include_all_scopes && data.scan_limit)
      bits.push(`reviewed the top ${data.scan_limit.toLocaleString("en-US")} of ${Number(data.candidates).toLocaleString("en-US")} by priority`);
    document.getElementById("contextNote").textContent = bits.join(" · ") + ".";
  }

  async function load() {
    const list = document.getElementById("list");
    list.className = ""; list.innerHTML = CIQ.skeletonRows(4);
    const params = new URLSearchParams();
    Object.entries(filters).forEach(([k, v]) => { if (v) params.set(k, v); });
    params.set("page", page); params.set("page_size", 24);
    let data;
    try { data = await CIQ.api.get("/api/sales/opportunities?" + params.toString()); }
    catch (e) { list.innerHTML = CIQ.errorBanner(e.message); return; }
    note(data);
    if (!data.items.length) {
      list.innerHTML = CIQ.emptyState({ icon: "◎", title: "No opportunities in this view",
        text: data.empty_message || "No opportunities match." });
      document.getElementById("pager").innerHTML = ""; return;
    }
    list.className = "grid-cards";
    list.innerHTML = data.items.map(card).join("");
    list.querySelectorAll("button[data-cid]").forEach((b) => b.addEventListener("click", () =>
      CIQ.logActivity({ companyId: Number(b.dataset.cid), companyName: b.dataset.name,
        projects: [{ project_id: b.dataset.pid, job_address: b.dataset.addr }],
        prefill: { subject: b.dataset.addr || "" }, onSaved: load })));
    CIQ.pager(document.getElementById("pager"), { page: data.page, pages: data.pages, total: data.total,
      onPage: (p) => { page = p; load(); window.scrollTo(0, 0); } });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("projects.view_assigned",
      { title: "Opportunities", subtitle: "Projects that created the opening", active: "opportunities.html" });
    if (!user) return;
    canEdit = CIQ.hasPerm("crm.activities.create");
    const incoming = new URLSearchParams(location.search);

    // Context: explicit from the link (dashboards pass it); otherwise the
    // user's own dashboard context. Organization needs companies.view.
    const ctxSel = document.getElementById("fcontext");
    const orgAllowed = CIQ.hasPerm("companies.view");
    if (orgAllowed) {
      const o = document.createElement("option"); o.value = "organization"; o.textContent = "Organization (last 60 days)";
      ctxSel.appendChild(o);
    }
    let ctx = incoming.get("context") || (user.dashboard_mode === "organization" ? "organization" : "assigned");
    if (ctx === "organization" && !orgAllowed) ctx = "assigned";
    ctxSel.value = ctx; filters.context = ctx;
    ctxSel.addEventListener("change", () => { filters.context = ctxSel.value; page = 1; load(); });

    const scopeBox = document.getElementById("fscope");
    if (incoming.get("scope") === "all") { scopeBox.checked = true; filters.scope = "all"; }
    scopeBox.addEventListener("change", () => { filters.scope = scopeBox.checked ? "all" : ""; page = 1; load(); });

    const lc = document.getElementById("flifecycle");
    ["preconstruction", "permitting", "under_construction", "inspection", "completed"].forEach((v) => {
      const o = document.createElement("option"); o.value = v; o.textContent = CIQ.titleCase(v); lc.appendChild(o);
    });
    const fq = document.getElementById("fq");
    fq.addEventListener("input", CIQ.debounce(() => { filters.q = fq.value.trim(); page = 1; load(); }, 350));
    lc.addEventListener("change", () => { filters.lifecycle = lc.value; page = 1; load(); });
    document.getElementById("fscore").addEventListener("change", (e) => { filters.score_min = e.target.value; page = 1; load(); });

    if (incoming.get("q")) { fq.value = incoming.get("q"); filters.q = incoming.get("q"); }
    if (incoming.get("lifecycle")) { lc.value = incoming.get("lifecycle"); filters.lifecycle = incoming.get("lifecycle"); }
    if (incoming.get("score_min")) {
      document.getElementById("fscore").value = incoming.get("score_min");
      filters.score_min = incoming.get("score_min");
    }
    load();
  });
})();
