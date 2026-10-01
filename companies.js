/* CorridorIQ — Company Intelligence directory.
   API-first (paginated, filterable) with a static JSON fallback. */

const API_BASE = "http://127.0.0.1:8770";
let apiLive = false;
let page = 1;
let pageSize = 50;
let lastPayload = null;

function fmtNum(v) { return v === null || v === undefined ? "—" : Number(v).toLocaleString(); }
function fmtScore(v) { return v === null || v === undefined ? "—" : Number(v).toFixed(0); }
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function probeApi() {
  const el = document.getElementById("apiStatus");
  try {
    const res = await fetch(`${API_BASE}/api/health`, { cache: "no-store" });
    apiLive = res.ok;
  } catch { apiLive = false; }
  el.textContent = apiLive
    ? "API online — live filtering, pagination, and match review enabled."
    : "API offline — showing static snapshot (top companies). Start: python -m pipeline.company_resolution.api";
  return apiLive;
}

function currentFilters() {
  return {
    q: document.getElementById("searchInput").value.trim(),
    role: document.getElementById("roleFilter").value,
    tier: document.getElementById("tierFilter").value,
    municipality: document.getElementById("municipalityFilter").value,
    active: document.getElementById("activeToggle").checked ? "1" : "",
    commercial: document.getElementById("commercialToggle").checked ? "1" : "",
    residential: document.getElementById("residentialToggle").checked ? "1" : "",
  };
}

async function fetchCompanies() {
  if (apiLive) {
    const f = currentFilters();
    const params = new URLSearchParams({ ...f, page: String(page), page_size: String(pageSize) });
    const res = await fetch(`${API_BASE}/api/companies?${params}`, { cache: "no-store" });
    return res.json();
  }
  // Static fallback: fetch snapshot once, filter client-side.
  const res = await fetch("data/exports/companies.json", { cache: "no-store" });
  const data = await res.json();
  const f = currentFilters();
  let items = data.items || [];
  if (f.q) items = items.filter((c) => (c.display_name || "").toLowerCase().includes(f.q.toLowerCase()));
  if (f.role) items = items.filter((c) => (c.roles || []).includes(f.role));
  if (f.tier) items = items.filter((c) => c.company_priority_tier === f.tier);
  if (f.active) items = items.filter((c) => (c.active_projects || 0) > 0);
  if (f.commercial) items = items.filter((c) => (c.commercial_project_count || 0) > 0);
  if (f.residential) items = items.filter((c) => (c.residential_project_count || 0) > 0);
  const total = items.length;
  const start = (page - 1) * pageSize;
  return {
    total, page, page_size: pageSize,
    pages: Math.max(1, Math.ceil(total / pageSize)),
    items: items.slice(start, start + pageSize),
    _static: data,
  };
}

function renderStats(payload) {
  const grid = document.getElementById("statTileGrid");
  const snap = payload._static || {};
  const total = snap.total_companies ?? payload.total ?? 0;
  const pending = snap.pending_matches ?? "—";
  const tiles = [
    ["Total companies", fmtNum(total)],
    ["Matching this filter", fmtNum(payload.total)],
    ["Pending identity reviews", fmtNum(pending)],
  ];
  grid.innerHTML = tiles.map(([label, value]) => `
    <div class="stat-tile">
      <p class="stat-tile-value">${value}</p>
      <p class="stat-tile-label">${label}</p>
    </div>`).join("");
}

function renderTable(payload) {
  const tbody = document.querySelector("#companiesTable tbody");
  const rows = payload.items || [];
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:24px;">No companies match these filters.</td></tr>`;
  } else {
    tbody.innerHTML = rows.map((c) => {
      const tier = c.company_priority_tier || "Low";
      const trend = c.activity_trend || "steady";
      const comm = c.commercial_project_count || 0;
      const res = c.residential_project_count || 0;
      const roles = (c.roles || []).slice(0, 3).map((r) => `<span class="role-chip">${esc(r)}</span>`).join("");
      return `<tr>
        <td><a class="company-name-link" href="company-profile.html?id=${c.id}">${esc(c.display_name || "—")}</a>
            <div>${roles}</div></td>
        <td>${esc(c.company_type_primary || "—")}</td>
        <td><span class="tier-badge tier-${esc(tier)}">${fmtScore(c.company_priority_score)} · ${esc(tier)}</span>
            ${c.account_relevance ? `<div class="muted" style="font-size:12px">${esc(c.account_relevance.label)}</div>` : ""}</td>
        <td>${fmtNum(c.active_projects)}</td>
        <td>${fmtNum(c.projects_last_30_days)}</td>
        <td>${fmtScore(c.average_opportunity_score)}</td>
        <td>${fmtNum(c.municipality_count)}</td>
        <td>${comm} / ${res}</td>
        <td class="trend-${esc(trend)}">${esc(trend)}</td>
        <td>${esc(c.latest_activity_date || "—")}</td>
      </tr>`;
    }).join("");
  }
  document.getElementById("resultCount").textContent =
    `${fmtNum(payload.total)} companies · page ${payload.page} of ${payload.pages}`;
  document.getElementById("pageInfo").textContent = `Page ${payload.page} / ${payload.pages}`;
  document.getElementById("prevPage").disabled = payload.page <= 1;
  document.getElementById("nextPage").disabled = payload.page >= payload.pages;
}

async function populateMunicipalities() {
  const sel = document.getElementById("municipalityFilter");
  try {
    const res = await fetch("data/exports/dashboard_summary.json", { cache: "no-store" });
    const data = await res.json();
    const list = (data.jurisdictions || data.connected || []).map((j) => j.slug || j).filter(Boolean);
    list.forEach((slug) => {
      const opt = document.createElement("option");
      opt.value = slug; opt.textContent = slug;
      sel.appendChild(opt);
    });
  } catch { /* optional */ }
}

async function refresh() {
  const payload = await fetchCompanies();
  lastPayload = payload;
  renderStats(payload);
  renderTable(payload);
  document.getElementById("generatedAt").textContent =
    payload._static ? `Snapshot ${payload._static.generated_at || ""}` : "Live";
}

function wire() {
  const debounced = (() => { let t; return (fn) => { clearTimeout(t); t = setTimeout(fn, 250); }; })();
  document.getElementById("searchInput").addEventListener("input", () => debounced(() => { page = 1; refresh(); }));
  ["roleFilter", "tierFilter", "municipalityFilter"].forEach((id) =>
    document.getElementById(id).addEventListener("change", () => { page = 1; refresh(); }));
  ["activeToggle", "commercialToggle", "residentialToggle"].forEach((id) =>
    document.getElementById(id).addEventListener("change", () => { page = 1; refresh(); }));
  document.getElementById("prevPage").addEventListener("click", () => { if (page > 1) { page--; refresh(); } });
  document.getElementById("nextPage").addEventListener("click", () => {
    if (lastPayload && page < lastPayload.pages) { page++; refresh(); }
  });
}

async function init() {
  if (location.protocol === "file:") {
    document.getElementById("apiStatus").textContent =
      "Open via a local web server (not file://) so data can load. e.g. python -m http.server";
  }
  await probeApi();
  await populateMunicipalities();
  wire();
  await refresh();
}

document.addEventListener("DOMContentLoaded", init);
