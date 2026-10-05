/* Owner/admin CEO Morning Brief. The API enforces access; this page only renders it. */
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

  function render(data) {
    const state = document.getElementById("briefState");
    const host = document.getElementById("brief");
    if (!data || data.available !== true) {
      state.innerHTML = "";
      host.innerHTML = CIQ.emptyState({
        icon: "☀",
        title: (data && data.message) || "No CEO Morning Brief is available yet.",
        text: "A new brief is stored only after a successful morning refresh.",
      });
      return;
    }
    const prov = data.provenance || {};
    const badge = data.current
      ? `<span class="badge green">Current</span>`
      : `<span class="badge amber">Older brief</span>`;
    const feeds = (data.feeds_stale || []).length
      ? `<span class="badge amber">Stale feed</span>`
      : `<span class="badge green">Feeds in brief</span>`;
    state.innerHTML = `<div class="stack" style="align-items:flex-end;gap:8px">${badge}${feeds}</div>`;
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
    host.innerHTML = notice + head + sections;
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", {
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
