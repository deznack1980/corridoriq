/* Estimator work queue — assigned / ready estimate tasks only. */
(function () {
  let data = null;

  function renderKpis() {
    const k = data.kpis || {};
    document.getElementById("kpis").innerHTML = [
      ["queue_count", "In queue", ""],
      ["submitted_count", "Submitted", "good"],
    ].map(([key, label, cls]) =>
      `<div class="kpi ${cls}"><div class="kpi-val">${k[key] || 0}</div>
       <div class="kpi-label">${label}</div></div>`).join("");
  }

  function projectTable(items, emptyTitle) {
    if (!items || !items.length) {
      return CIQ.emptyState({ title: emptyTitle, text: "Nothing in this list right now." });
    }
    return `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
      <th>Permit</th><th>Company</th><th>Location</th><th>Lifecycle</th><th>Priority</th>
      </tr></thead><tbody>${items.map((r) => `<tr>
        <td data-label="Permit">${CIQ.esc(r.permit_number || "#" + r.project_id)}</td>
        <td data-label="Company">${CIQ.esc(r.company_name || "—")}</td>
        <td data-label="Location">${CIQ.esc([r.city, r.job_address].filter(Boolean).join(" · ") || "—")}</td>
        <td data-label="Lifecycle">${CIQ.esc(r.project_lifecycle || "—")}</td>
        <td data-label="Priority">${CIQ.bandBadge(r.opportunity_score)}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  async function load() {
    try {
      data = await CIQ.api.get("/api/estimator/work-queue");
    } catch (e) {
      document.getElementById("queue").innerHTML = CIQ.errorBanner(e.message);
      return;
    }
    renderKpis();
    document.getElementById("queue").innerHTML =
      projectTable(data.queue, "No projects in your estimate queue");
    document.getElementById("mine").innerHTML =
      projectTable(data.my_estimates, "No active estimate tasks");
    document.getElementById("submitted").innerHTML =
      projectTable(data.submitted_estimates, "No submitted estimates yet");
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("projects.view_assigned", {
      title: "Work Queue",
      subtitle: "Assigned estimate work",
      active: "estimator-work-queue.html",
      anyPerm: ["projects.view_assigned", "projects.view", "admin.system"],
    });
    if (!user) return;
    load();
  });
})();
