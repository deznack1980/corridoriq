/* Read-only dashboard — assignment-scoped, no write actions. */
(function () {
  async function load() {
    let data;
    try {
      data = await CIQ.api.get("/api/sales/dashboard");
    } catch (e) {
      document.getElementById("companies").innerHTML = CIQ.errorBanner(e.message);
      return;
    }
    const k = data.kpis || {};
    document.getElementById("kpis").innerHTML = [
      ["my_companies", "Assigned companies"],
      ["high_priority_opportunities", "High-priority"],
      ["overdue_followups", "Overdue follow-ups"],
    ].map(([key, label]) =>
      `<div class="kpi"><div class="kpi-val">${k[key] || 0}</div>
       <div class="kpi-label">${label}</div></div>`).join("");

    const cos = data.priority_companies || [];
    const el = document.getElementById("companies");
    if (!cos.length) {
      el.innerHTML = CIQ.emptyState({
        title: "No companies assigned to you",
        text: "Ask a manager to assign companies if you need visibility.",
      });
    } else {
      el.innerHTML = `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
        <th>Company</th><th>Tier</th><th>Status</th><th>Next follow-up</th>
        </tr></thead><tbody>${cos.map((c) => `<tr>
          <td data-label="Company"><a href="sales-company-profile.html?id=${c.company_id}">${CIQ.esc(c.display_name)}</a></td>
          <td data-label="Tier">${CIQ.esc(c.company_priority_tier || "—")}</td>
          <td data-label="Status">${CIQ.esc(c.relationship_status || "—")}</td>
          <td data-label="Follow-up">${c.next_followup_at ? CIQ.fmtDate(c.next_followup_at) : "—"}</td>
        </tr>`).join("")}</tbody></table></div>`;
    }

    const opps = data.recent_opportunity_activity || [];
    const oel = document.getElementById("opps");
    if (!opps.length) {
      oel.innerHTML = CIQ.emptyState({ title: "No recent opportunity activity" });
    } else {
      oel.innerHTML = `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
        <th>Company</th><th>Lifecycle</th><th>Priority</th>
        </tr></thead><tbody>${opps.map((o) => `<tr>
          <td data-label="Company">${CIQ.esc(o.display_name || "—")}</td>
          <td data-label="Lifecycle">${CIQ.esc(o.project_lifecycle || "—")}</td>
          <td data-label="Priority">${CIQ.bandBadge(o.opportunity_score)}</td>
        </tr>`).join("")}</tbody></table></div>`;
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("crm.relationships.view", {
      title: "Dashboard",
      subtitle: "Read-only view",
      active: "readonly-dashboard.html",
    });
    if (!user) return;
    load();
  });
})();
