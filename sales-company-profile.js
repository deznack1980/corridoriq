/* Company profile — read-only intelligence clearly separated from editable CRM. */
(function () {
  const companyId = Number(CIQ.qs("id"));
  let detail = null, projects = [], permits = [], activities = [], tasks = [];
  let canEdit = false, repMap = {};

  async function loadAll() {
    const [d, pj, pm, ac] = await Promise.all([
      CIQ.api.get(`/api/sales/companies/${companyId}`),
      CIQ.api.get(`/api/sales/companies/${companyId}/projects`).catch(() => ({ items: [] })),
      CIQ.api.get(`/api/sales/companies/${companyId}/permits`).catch(() => ({ items: [] })),
      CIQ.api.get(`/api/sales/companies/${companyId}/activities`).catch(() => ({ items: [] })),
    ]);
    detail = d; projects = pj.items || pj || []; permits = pm.items || pm || [];
    activities = ac.items || ac || [];
    try {
      const t = await CIQ.api.get("/api/sales/tasks");
      tasks = (t.items || []).filter((x) => x.company_id === companyId);
    } catch (e) { tasks = []; }
  }

  function repName(id) {
    if (!id) return "Unassigned";
    if (id === CIQ.user.id) return "You";
    return repMap[id] || "Team member";
  }

  function header() {
    const c = detail.company, ci = detail.intelligence || {}, r = detail.relationship || {};
    const loc = [c.city, c.state].filter(Boolean).join(", ") || "—";
    // Only this company's own verified values drive the Call / Email links.
    const vc = detail.verified_contact;
    const verified = vc && vc.status === "VERIFIED";
    const phone = verified && vc.phone ? vc.phone.value : null;
    const email = verified && vc.email ? vc.email.value : null;
    return `<div class="card card-pad">
      <div class="profile-header">
        <div>
          <h1 style="font-size:23px">${CIQ.esc(c.display_name || c.legal_name || "Company")}</h1>
          <div class="ph-meta">
            <span>${CIQ.esc(loc)}</span>
            <span>${CIQ.priorityBadge(ci.company_priority_tier)}</span>
            <span>${CIQ.statusBadge(r.relationship_status || "new")}</span>
            <span>Rep: <b>${CIQ.esc(repName(r.assigned_user_id))}</b></span>
          </div>
          <div class="ph-meta">
            <span>Last contact: <b>${r.last_contact_at ? CIQ.relTime(r.last_contact_at) : "Never"}</b></span>
            <span>Next follow-up: <b>${r.next_followup_at ? CIQ.fmtDate(r.next_followup_at) : "—"}</b></span>
          </div>
          <h4 style="margin:10px 0 4px">Verified contact</h4>
          ${CIQ.verifiedContactBlock(vc)}
        </div>
      </div>
      <div class="action-bar">
        ${phone ? `<a class="btn btn-primary btn-sm" href="tel:${CIQ.esc(phone)}" data-a="call">Call</a>`
          : `<button class="btn btn-primary btn-sm" data-a="call">Call</button>`}
        ${email ? `<a class="btn btn-sm" href="mailto:${CIQ.esc(email)}" data-a="email">Email</a>`
          : `<button class="btn btn-sm" data-a="email">Email</button>`}
        <button class="btn btn-sm" data-a="log">Log activity</button>
        ${canEdit ? `<button class="btn btn-sm" data-a="task">Create task</button>
        <button class="btn btn-sm" data-a="followup">Schedule follow-up</button>
        <button class="btn btn-sm" data-a="status">Update status</button>` : ""}
      </div>
    </div>`;
  }

  function crmSection() {
    const r = detail.relationship || {};
    const last = activities[0];
    const notes = activities.filter((a) => a.notes).slice(0, 3);
    const openTasks = tasks.filter((t) => t.status === "open" || t.status === "in_progress");
    const quote = ["quote_requested", "quote_sent"].includes(r.relationship_status)
      ? CIQ.statusLabel(r.relationship_status) : "None";
    return `<div class="card">
      <div class="card-head"><h2>Sales relationship</h2>${CIQ.statusBadge(r.relationship_status || "new")}</div>
      <div class="card-pad">
        <dl class="kv">
          <dt>Assigned to</dt><dd>${CIQ.esc(repName(r.assigned_user_id))}</dd>
          <dt>Last activity</dt><dd>${last ? CIQ.activityLabel(last.activity_type) + " · " + CIQ.relTime(last.activity_at) : "—"}</dd>
          <dt>Last outcome</dt><dd>${last && last.activity_outcome ? CIQ.esc(CIQ.outcomeLabel(last.activity_outcome)) : "—"}</dd>
          <dt>Next follow-up</dt><dd>${r.next_followup_at ? CIQ.fmtDate(r.next_followup_at) : "—"}</dd>
          <dt>Open tasks</dt><dd>${openTasks.length}</dd>
          <dt>Quote status</dt><dd>${CIQ.esc(quote)}</dd>
        </dl>
        ${notes.length ? `<div style="margin-top:12px"><div class="muted" style="font-size:12px;margin-bottom:4px">Recent notes</div>
          ${notes.map((n) => `<div style="font-size:13px;border-left:2px solid var(--border);padding:2px 0 6px 10px;margin-bottom:4px">${CIQ.esc(n.notes)}</div>`).join("")}</div>` : ""}
      </div>
    </div>`;
  }

  function intelSection() {
    const ci = detail.intelligence || {};
    const comm = ci.commercial_project_count || 0, res = ci.residential_project_count || 0;
    const tot = comm + res, cpct = tot ? Math.round(comm / tot * 100) : 0;
    const metric = (v, l) => `<div class="metric"><div class="v">${v == null ? "—" : v}</div><div class="l">${l}</div></div>`;
    return `<div class="card">
      <div class="card-head"><h2>Company intelligence</h2><span class="badge slate">Read-only</span></div>
      <div class="card-pad">
        <div class="metric-grid">
          ${metric(ci.total_projects, "Total projects")}
          ${metric(ci.active_projects, "Active projects")}
          ${metric(ci.projects_last_30_days, "Last 30 days")}
          ${metric(ci.municipality_count, "Municipalities")}
          ${metric(ci.highest_opportunity_score != null ? CIQ.bandBadge(ci.highest_opportunity_score) : null, "Top project")}
          ${metric(ci.projects_last_90_days, "Last 90 days")}
        </div>
        ${tot ? `<div style="margin-top:14px">
          <div class="spread" style="font-size:12.5px;color:var(--text-3)"><span>Commercial ${comm}</span><span>Residential ${res}</span></div>
          <div class="progress" style="margin-top:5px"><span style="width:${cpct}%"></span></div>
        </div>` : ""}
        ${ci.activity_trend ? `<div class="muted" style="margin-top:12px;font-size:13px">Activity trend: <b>${CIQ.esc(CIQ.titleCase(ci.activity_trend))}</b></div>` : ""}
      </div>
    </div>`;
  }

  function projectCard(p) {
    return `<div class="company-card">
      <div class="cc-top">
        <div><div class="cc-name" style="font-size:14.5px">${CIQ.esc(p.job_address || p.permit_number || p.jurisdiction || "Project")}</div>
          <div class="cc-loc">${CIQ.esc([p.jurisdiction, p.city].filter(Boolean).join(" · ") || "")}</div></div>
        ${CIQ.bandBadge(p.opportunity_score)}
      </div>
      <div class="cc-meta">
        <div><span>Stage</span>${CIQ.esc(CIQ.titleCase(p.project_lifecycle || "—"))}</div>
        <div><span>Category</span>${CIQ.esc(CIQ.titleCase(p.project_category || "—"))}</div>
        <div><span>Opportunity</span>${p.opportunity_date ? CIQ.fmtDate(p.opportunity_date) : "—"}</div>
        <div><span>Est. value</span>${p.estimated_material_value != null ? CIQ.money(p.estimated_material_value) : "—"}</div>
      </div>
      <div class="cc-quick">
        <button class="btn btn-sm" data-p="${p.project_id}" data-a="pdetail">View details</button>
        ${canEdit ? `<button class="btn btn-sm btn-ghost" data-p="${p.project_id}" data-addr="${CIQ.esc(p.job_address || "")}" data-a="plog">Log activity</button>` : ""}
      </div>
    </div>`;
  }

  function oppsSection() {
    if (!projects.length) {
      return `<div class="section-title">Current opportunities</div>` +
        CIQ.emptyState({ title: "No projects on record", text: "No permits or projects are linked to this company yet." });
    }
    return `<div class="section-title">Current opportunities <span class="badge slate">${projects.length}</span></div>
      <div class="grid-cards" id="oppCards">${projects.slice(0, 12).map(projectCard).join("")}</div>`;
  }

  function timelineSection() {
    if (!activities.length) {
      return `<div class="section-title">Activity timeline</div>` +
        CIQ.emptyState({ icon: "≣", title: "No activity logged yet",
          text: canEdit ? "Log your first call, email, or note using the buttons above." : "" });
    }
    return `<div class="section-title">Activity timeline</div>
      <div class="card card-pad"><div class="timeline">${activities.map((a) => `
        <div class="tl-item ${CIQ.esc(a.activity_type)}">
          <div class="tl-head">
            <span class="tl-type">${CIQ.esc(CIQ.activityLabel(a.activity_type))}</span>
            ${a.activity_outcome ? `<span class="badge slate">${CIQ.esc(CIQ.outcomeLabel(a.activity_outcome))}</span>` : ""}
            <span class="tl-when">${CIQ.fmtDateTime(a.activity_at)}</span>
          </div>
          ${a.subject ? `<div style="font-size:13px;font-weight:600">${CIQ.esc(a.subject)}</div>` : ""}
          ${a.notes ? `<div class="tl-notes">${CIQ.esc(a.notes)}</div>` : ""}
          ${a.next_followup_at ? `<div class="muted" style="font-size:12px;margin-top:3px">Next follow-up: ${CIQ.fmtDate(a.next_followup_at)}</div>` : ""}
        </div>`).join("")}</div></div>`;
  }

  function dataDetails() {
    const dq = detail.data_quality || {}, roles = detail.roles || [], aliases = detail.aliases || [];
    const contacts = detail.contacts || [];
    return `<details class="data-details" style="margin-top:26px">
      <summary>Additional intelligence details</summary>
      <div class="card-pad">
        ${contacts.length ? `<h4 style="margin-bottom:6px">Contacts</h4>
          <div class="table-wrap" style="margin-bottom:14px"><table class="tbl"><thead><tr><th>Name</th><th>Title</th><th>Phone</th><th>Email</th></tr></thead>
          <tbody>${contacts.map((c) => `<tr><td>${CIQ.esc(c.full_name || "")}</td><td>${CIQ.esc(c.job_title || "")}</td>
            <td>${CIQ.esc(c.phone || "")}</td><td>${CIQ.esc(c.email || "")}</td></tr>`).join("")}</tbody></table></div>` : ""}
        ${roles.length ? `<h4 style="margin-bottom:6px">Roles</h4><div class="row" style="margin-bottom:14px">${roles.map((r) =>
          `<span class="badge slate">${CIQ.esc(CIQ.titleCase(r.role_type))}${r.is_primary ? " · primary" : ""}</span>`).join("")}</div>` : ""}
        ${aliases.length ? `<h4 style="margin-bottom:6px">Also known as</h4><div class="muted" style="font-size:13px;margin-bottom:14px">${aliases.map((a) => CIQ.esc(a.alias_name)).join(", ")}</div>` : ""}
        <h4 style="margin-bottom:6px">Raw permits (${permits.length})</h4>
        <div class="table-wrap"><table class="tbl"><thead><tr><th>Permit</th><th>Type</th><th>Status</th><th>Address</th><th>Issued</th></tr></thead>
        <tbody>${permits.slice(0, 40).map((p) => `<tr>
          <td>${CIQ.esc(p.permit_number || "")}</td><td>${CIQ.esc(p.permit_type || "")}</td>
          <td>${CIQ.esc(p.status || "")}</td><td>${CIQ.esc(p.job_address || p.city || "")}</td>
          <td>${p.issued_date ? CIQ.fmtDate(p.issued_date) : "—"}</td></tr>`).join("")}</tbody></table></div>
        <div class="muted" style="font-size:12px;margin-top:10px">Potential duplicate records: ${dq.potential_duplicates != null ? dq.potential_duplicates : "—"} ·
          Source records: ${dq.source_record_count != null ? dq.source_record_count : "—"}</div>
      </div>
    </details>`;
  }

  function render() {
    const root = document.getElementById("root");
    root.innerHTML = header()
      + `<div class="two-col" style="margin-top:18px">${crmSection()}${intelSection()}</div>`
      + oppsSection() + timelineSection() + dataDetails();
    wireActions();
  }

  function openStatusModal() {
    const r = detail.relationship || {};
    const opts = [["contacted", "Contacted"], ["qualified", "Qualified"], ["follow_up", "Follow-Up"],
      ["quote_requested", "Quote Requested"], ["quote_sent", "Quote Sent"], ["negotiating", "Negotiating"],
      ["won", "Won"], ["lost", "Lost"], ["do_not_contact", "Do Not Contact"], ["inactive", "Inactive"]];
    const body = document.createElement("div");
    body.innerHTML = `<label class="fld">New status
      <select id="stSel">${opts.map(([v, l]) => `<option value="${v}" ${r.relationship_status === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label class="fld" style="margin-top:12px">Note (optional)<textarea id="stNote" placeholder="Why?"></textarea></label>`;
    const foot = document.createElement("div");
    foot.innerHTML = `<button class="btn" data-a="cancel">Cancel</button><button class="btn btn-primary" data-a="save">Update</button>`;
    const m = CIQ.modal({ title: "Update relationship status", body, footer: foot });
    foot.querySelector('[data-a="cancel"]').addEventListener("click", m.close);
    foot.querySelector('[data-a="save"]').addEventListener("click", async (e) => {
      const status = body.querySelector("#stSel").value;
      const note = body.querySelector("#stNote").value.trim();
      if (status === "lost" || status === "do_not_contact") {
        const ok = await CIQ.confirm(status === "lost" ? "Mark this company as Lost?" : "Set Do Not Contact?",
          { danger: true, confirmLabel: "Confirm" });
        if (!ok) return;
      }
      await CIQ.busy(e.target, async () => {
        try {
          await CIQ.api.patch(`/api/sales/companies/${companyId}/relationship`,
            { relationship_status: status, status_note: note || null });
          CIQ.toast("Status updated", "success"); m.close(); await loadAll(); render();
        } catch (err) { CIQ.toast(err.message, "error"); }
      });
    });
  }

  function openTaskModal(prefill) {
    const body = document.createElement("form");
    body.className = "form-grid";
    body.innerHTML = `<label class="fld full">Title<input name="title" required value="${CIQ.esc((prefill && prefill.title) || "")}" /></label>
      <label class="fld">Priority<select name="priority"><option value="normal">Normal</option><option value="high">High</option>
        <option value="urgent">Urgent</option><option value="low">Low</option></select></label>
      <label class="fld">Due<input type="datetime-local" name="due_at" /></label>
      <label class="fld full">Description<textarea name="description"></textarea></label>`;
    const foot = document.createElement("div");
    foot.innerHTML = `<button type="button" class="btn" data-a="cancel">Cancel</button>
      <button type="button" class="btn btn-primary" data-a="save">Create task</button>`;
    const m = CIQ.modal({ title: "Create task", body, footer: foot });
    foot.querySelector('[data-a="cancel"]').addEventListener("click", m.close);
    foot.querySelector('[data-a="save"]').addEventListener("click", async (e) => {
      const fd = new FormData(body);
      if (!(fd.get("title") || "").trim()) { CIQ.toast("Title is required", "error"); return; }
      await CIQ.busy(e.target, async () => {
        try {
          await CIQ.api.post("/api/sales/tasks", {
            company_id: companyId, title: fd.get("title").trim(),
            priority: fd.get("priority"), description: fd.get("description") || null,
            due_at: fd.get("due_at") ? fd.get("due_at") + ":00" : null,
          });
          CIQ.toast("Task created", "success"); m.close(); await loadAll(); render();
        } catch (err) { CIQ.toast(err.message, "error"); }
      });
    });
  }

  function projectDetailModal(pid) {
    const p = projects.find((x) => String(x.project_id) === String(pid));
    if (!p) return;
    const rows = [["Address", p.job_address], ["Municipality", p.jurisdiction], ["City", p.city],
      ["Lifecycle stage", CIQ.titleCase(p.project_lifecycle || "")], ["Category", CIQ.titleCase(p.project_category || "")],
      ["Priority", CIQ.oppBand(p.opportunity_score).label],
      ["Opportunity timing", CIQ.titleCase(p.opportunity_timing || "")],
      ["Opportunity date", p.opportunity_date ? CIQ.fmtDate(p.opportunity_date) : "—"],
      ["Permit #", p.permit_number], ["Est. material value", p.estimated_material_value != null ? CIQ.money(p.estimated_material_value) : "—"]];
    const body = `<dl class="kv">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${CIQ.esc(v || "—")}</dd>`).join("")}</dl>`;
    const foot = canEdit ? `<button class="btn btn-primary" data-a="log">Log activity for this project</button>` : "";
    const m = CIQ.modal({ title: p.job_address || "Project detail", body, footer: foot || undefined });
    if (canEdit) m.el.querySelector('[data-a="log"]').addEventListener("click", () => {
      m.close();
      CIQ.logActivity({ companyId, companyName: detail.company.display_name, projects,
        prefill: { subject: p.job_address || "" }, onSaved: async () => { await loadAll(); render(); } });
    });
  }

  function wireActions() {
    const root = document.getElementById("root");
    const doLog = (prefill) => CIQ.logActivity({
      companyId, companyName: detail.company.display_name, projects, prefill,
      onSaved: async () => { await loadAll(); render(); } });
    root.querySelectorAll("[data-a]").forEach((b) => b.addEventListener("click", (e) => {
      const a = b.dataset.a;
      if (a === "call" && b.tagName === "A") return;       // tel: link
      if (a === "email" && b.tagName === "A") return;      // mailto: link
      if (a === "call") doLog({ activity_type: "call" });
      else if (a === "email") doLog({ activity_type: "email" });
      else if (a === "log") doLog({});
      else if (a === "followup") doLog({ activity_type: "follow_up" });
      else if (a === "status") openStatusModal();
      else if (a === "task") openTaskModal();
      else if (a === "pdetail") projectDetailModal(b.dataset.p);
      else if (a === "plog") doLog({ subject: b.dataset.addr || "" });
    }));
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("crm.relationships.view", { title: "Company", active: "my-companies.html" });
    if (!user) return;
    canEdit = CIQ.hasPerm("crm.activities.create") || CIQ.hasPerm("crm.relationships.update");
    if (CIQ.hasPerm("users.view")) {
      try { (await CIQ.api.get("/api/manager/team")).items.forEach((u) => { repMap[u.user_id] = u.display_name; }); }
      catch (e) {}
    }
    try { await loadAll(); }
    catch (e) {
      document.getElementById("root").innerHTML = CIQ.errorBanner(
        e.status === 403 ? "You don't have access to this company." : (e.message || "Could not load company."));
      return;
    }
    render();
  });
})();
