/* Owner-only CEO Morning Brief (owner.ceo_agent). The server enforces access to
   this page and its API; this page only renders it. */
(function () {
  const CLAIM_COLOR = {
    FACT: "green",
    CALCULATION: "blue",
    INFERENCE: "amber",
    RECOMMENDATION: "purple",
    UNKNOWN: "slate",
  };

  function meta(label, value) {
    return `<div><span>${CIQ.esc(label)}</span>${CIQ.esc(value || "UNKNOWN")}</div>`;
  }

  const HEALTH_BADGE = {
    healthy: "green",
    warning: "amber",
    critical: "red",
    unknown: "amber",
  };

  function renderHealth(health) {
    if (!health) return "";
    const tone = HEALTH_BADGE[health.overall_status] || "amber";
    const parts = health.components || {};
    const componentLine = ["refresh", "database", "feeds", "downstream", "application", "billing"].map((name) => {
      const item = parts[name] || {};
      const shown = name === "billing" && item.status === "not_applicable"
        ? "NOT ENABLED"
        : (item.status || "unknown");
      return meta(name, shown);
    }).join("");
    const incidents = (health.incidents || []).filter((item) => item.severity !== "healthy");
    const attention = incidents.length ? `<div class="section-title">ATTENTION REQUIRED</div>` + incidents.map((item) => {
      const badge = HEALTH_BADGE[item.severity] || "amber";
      return `<div class="card card-pad" style="margin-bottom:10px">
        <div><span class="badge ${badge}">${CIQ.esc(item.severity || "unknown")}</span>
        <strong>${CIQ.esc(item.source || item.component || "")}</strong></div>
        <p>${CIQ.esc(item.fact || "")}</p>
        <p>${CIQ.esc(item.impact || "")}</p>
        <p>${CIQ.esc(item.recommended_action || "")}</p>
      </div>`;
    }).join("") : "";
    const notice = health.artifact_notice
      ? `<div class="card card-pad" style="margin-top:12px">${CIQ.esc(health.artifact_notice)}</div>`
      : "";
    return `<div class="section-title">SYSTEM HEALTH
        <span class="badge ${tone}">${CIQ.esc(health.label || "CHECK INCOMPLETE — REVIEW NEEDED")}</span></div>
      <div class="card card-pad"><div class="cc-meta">
        ${meta("Checked at", health.checked_at)}
        ${meta("Alert", health.owner_alert_required ? "Owner review required" : "No owner alert")}
        ${componentLine}
      </div></div>${notice}${attention}`;
  }

  function render(data) {
    const state = document.getElementById("briefState");
    const host = document.getElementById("brief");
    const health = data && data.health;
    const tone = health ? (HEALTH_BADGE[health.overall_status] || "amber") : "amber";
    state.innerHTML = `<span class="badge ${tone}">${CIQ.esc((health && health.label) || "CHECK INCOMPLETE — REVIEW NEEDED")}</span>`;
    if (!data || data.available !== true) {
      host.innerHTML = renderHealth(health) + CIQ.emptyState({
        icon: "☀",
        title: (data && data.message) || "No CEO Morning Brief is available yet.",
        text: "A new brief is stored only after a successful morning refresh.",
      });
      return;
    }
    const prov = data.provenance || {};
    const badge = data.current
      ? `<span class="badge green">Current brief</span>`
      : `<span class="badge amber">Older brief</span>`;
    state.innerHTML = `<div class="stack" style="align-items:flex-end;gap:8px"><span class="badge ${tone}">${CIQ.esc((health && health.label) || "CHECK INCOMPLETE — REVIEW NEEDED")}</span>${badge}</div>`;
    const notice = data.notice
      ? `<div class="card card-pad" style="margin-top:16px"><strong>Not the latest refresh.</strong> ${CIQ.esc(data.notice)}</div>`
      : "";
    const head = `<div class="card card-pad" style="margin-top:16px"><div class="cc-meta">
      ${meta("Data current through", prov.data_as_of)}
      ${meta("Last successful refresh", prov.latest_successful_refresh)}
      ${meta("Brief generated at", prov.generated_at)}
      ${meta("Refresh run", prov.refresh_run_id == null ? "UNKNOWN" : String(prov.refresh_run_id))}
    </div></div>`;
    const sections = (data.sections || []).map((section) => {
      const color = CLAIM_COLOR[section.claim_class] || "slate";
      return `<div class="section-title">${CIQ.esc(section.heading || "")}
        <span class="badge ${color}">${CIQ.esc(section.claim_class || "UNKNOWN")}</span></div>
        <div class="card card-pad" style="white-space:pre-wrap">${CIQ.esc(section.text || "")}</div>`;
    }).join("");
    host.innerHTML = renderHealth(health) + notice + head + sections;
  }

  document.addEventListener("DOMContentLoaded", async () => {
    // Owner only. The server refuses the page and the API to everyone else.
    const user = await CIQ.guard("owner.ceo_agent", {
      title: "CEO Morning Brief",
      subtitle: "Owner only",
      active: "ceo-morning-brief.html",
    });
    if (!user) return;
    try {
      render(await CIQ.api.get("/api/admin/ceo-morning-brief"));
    } catch (e) {
      render({ available: false, message: "No CEO Morning Brief is available yet." });
    }
  });
})();
