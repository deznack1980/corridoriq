/* Phoenix opportunity map workspace (shared by the admin and sales dashboards).
 *
 * The map and the feed beside it come from the same server function
 * (crm.opportunities / crm.opportunity_map) with the same context and scope,
 * so they always describe the same set of trust-checked opportunities.
 * Only coordinates published by the permit source are placed; everything else
 * is counted as "location not available", never guessed.
 */
(function () {
  const PHOENIX = [33.45, -112.07];
  const CLUSTER_PX = 56;
  const FEED_SIZE = 25;
  const TRADE_COLORS = [
    [/plumbing|water heater/i, "#2563eb", "Plumbing / water heater"],
    [/fuel-gas|propane/i, "#ea580c", "Fuel gas / propane"],
    [/fire line|backflow/i, "#dc2626", "Fire line / backflow"],
    [/civil/i, "#0d9488", "Civil wet utility"],
    [/hvac|mechanical/i, "#7c3aed", "HVAC / mechanical"],
  ];
  const OTHER = ["#64748b", "Trades not stated"];

  function colorFor(scope) {
    for (const [re, color] of TRADE_COLORS) if (re.test(scope || "")) return color;
    return OTHER[0];
  }

  function contactHtml(c) {
    if (!c || c.status !== "VERIFIED") return `<span class="muted">${CIQ.esc((c && c.label) || "Contact not yet verified.")}</span>`;
    const who = [c.name, c.title].filter(Boolean).join(", ") || "Business line";
    const link = c.phone ? `<a href="tel:${CIQ.esc(c.phone)}">${CIQ.esc(c.phone)}</a>`
      : c.email ? `<a href="mailto:${CIQ.esc(c.email)}">${CIQ.esc(c.email)}</a>` : "";
    return `<span class="badge green">Verified</span> ${CIQ.esc(who)}${link ? " · " + link : ""}`;
  }

  function popupHtml(p) {
    const company = p.company_id
      ? `<a href="sales-company-profile.html?id=${p.company_id}"><b>${CIQ.esc(p.display_name || "Company")}</b></a>`
      : `<b>No contractor attributed</b>`;
    const approx = p.precision === "source_state_plane" ? `<div class="muted" style="font-size:11px">Location converted from the city's state-plane coordinates.</div>` : "";
    return `<div class="map-pop">${company}
      <div>${CIQ.esc(p.job_address || p.permit_number || "")} · ${CIQ.esc(p.jurisdiction || "")}</div>
      <div><span class="muted">Trade scope:</span> ${CIQ.esc(p.trade_scope || "—")}</div>
      <div><span class="muted">Score:</span> ${p.opportunity_score != null ? Math.round(p.opportunity_score) : "—"}
        · <span class="muted">Date:</span> ${p.opportunity_date ? CIQ.fmtDate(p.opportunity_date) : "—"}</div>
      <div style="margin-top:4px">${contactHtml(p.contact)}</div>${approx}</div>`;
  }

  CIQ.mapWorkspace = function (root, opts) {
    const o = opts || {};
    // Re-entry (dashboard reload after logging activity): keep the one map.
    if (root._mapWorkspace) {
      root._mapWorkspace.update(o);
      return root._mapWorkspace;
    }
    const state = { context: o.context || "assigned", scope: "", map: null, layer: null, points: [], today: [], markers: {} };
    const orgAllowed = CIQ.hasPerm("companies.view");
    root.innerHTML = `
      <div class="mw-head">
        <div><h2>Phoenix opportunity map</h2><div class="sub mw-summary">Loading…</div></div>
        <div class="mw-controls">
          <select class="mw-context">
            <option value="assigned">My / assigned companies</option>
            ${orgAllowed ? '<option value="organization">Organization (last 60 days)</option>' : ""}
          </select>
          <label class="muted"><input type="checkbox" class="mw-scope"> Include all scopes</label>
          <a class="btn btn-sm mw-viewall" href="#">View all</a>
        </div>
      </div>
      <div class="mw-grid">
        <div class="mw-map" role="region" aria-label="Opportunity map"></div>
        <div class="mw-feed"></div>
      </div>
      <div class="mw-foot muted"></div>
      <div class="mw-legend"></div>`;
    const $ = (sel) => root.querySelector(sel);
    $(".mw-context").value = state.context === "organization" && !orgAllowed ? "assigned" : state.context;
    state.context = $(".mw-context").value;
    $(".mw-context").addEventListener("change", (e) => { state.context = e.target.value; load(); });
    $(".mw-scope").addEventListener("change", (e) => { state.scope = e.target.checked ? "all" : ""; load(); });
    $(".mw-legend").innerHTML = TRADE_COLORS.map(([, c, l]) => `<span><i style="background:${c}"></i>${l}</span>`).join("")
      + `<span><i style="background:${OTHER[0]}"></i>${OTHER[1]}</span>`
      + `<span><i class="ring"></i>Account to act on today</span>`;

    if (!window.L) {
      $(".mw-map").innerHTML = CIQ.errorBanner("Map library failed to load.");
    } else {
      state.map = L.map($(".mw-map"), { scrollWheelZoom: true }).setView(PHOENIX, 10);
      L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
      }).addTo(state.map);
      state.layer = L.layerGroup().addTo(state.map);
      state.map.on("zoomend", draw);
    }

    function params(extra) {
      const q = new URLSearchParams({ context: state.context });
      if (state.scope) q.set("scope", "all");
      Object.entries(extra || {}).forEach(([k, v]) => q.set(k, v));
      return q.toString();
    }

    // Grid clustering in screen space; recomputed per zoom level.
    function draw() {
      if (!state.map) return;
      state.layer.clearLayers();
      state.markers = {};
      const z = state.map.getZoom();
      const cells = new Map();
      state.points.forEach((p) => {
        const px = state.map.project([p.lat, p.lon], z);
        const key = Math.floor(px.x / CLUSTER_PX) + ":" + Math.floor(px.y / CLUSTER_PX);
        if (!cells.has(key)) cells.set(key, []);
        cells.get(key).push(p);
      });
      cells.forEach((members) => {
        if (members.length === 1 || z >= 17) {
          members.forEach((p) => {
            const m = L.circleMarker([p.lat, p.lon], { radius: 7, weight: 1.5, color: "#fff",
              fillColor: colorFor(p.trade_scope), fillOpacity: 0.9 }).bindPopup(popupHtml(p));
            m.addTo(state.layer);
            state.markers[p.project_id] = m;
          });
          return;
        }
        const lat = members.reduce((s, p) => s + p.lat, 0) / members.length;
        const lon = members.reduce((s, p) => s + p.lon, 0) / members.length;
        const size = members.length < 10 ? 30 : members.length < 100 ? 38 : 46;
        const icon = L.divIcon({ className: "mw-cluster", iconSize: [size, size],
          html: `<div style="width:${size}px;height:${size}px;line-height:${size}px">${members.length}</div>` });
        const bounds = L.latLngBounds(members.map((p) => [p.lat, p.lon]));
        L.marker([lat, lon], { icon, title: `${members.length} opportunities` })
          .on("click", () => state.map.fitBounds(bounds.pad(0.3)))
          .addTo(state.layer);
      });
      state.today.forEach((a) => {
        const loc = a.project && a.project.location;
        if (!loc) return;
        L.circleMarker([loc.lat, loc.lon], { radius: 13, weight: 3, color: "#16a34a", fill: false })
          .bindPopup(`<div class="map-pop"><b>Today: ${CIQ.esc(a.display_name)}</b><div>${CIQ.esc(a.action)} · ${CIQ.esc(a.trade)}</div>
            <div style="margin-top:4px">${CIQ.esc(a.why_now)}</div></div>`)
          .addTo(state.layer);
      });
    }

    function feedHtml(feed) {
      if (!feed.items.length) {
        return CIQ.emptyState({ compact: true, icon: "◎", title: "No opportunities in this view",
          text: feed.empty_message || "No opportunities match." });
      }
      return `<div class="mw-feed-head muted">Top ${Math.min(FEED_SIZE, feed.items.length)} by score · same set as the map</div>` +
        feed.items.map((p) => `
        <div class="mw-item${p.location ? "" : " is-unplaced"}" data-pid="${p.project_id}">
          <div class="mw-item-top">
            <span class="mw-dot" style="background:${colorFor(p.trade_scope)}"></span>
            <b>${p.company_id ? CIQ.esc(p.display_name || "Company") : "No contractor attributed"}</b>
            <span class="score-chip">${p.opportunity_score != null ? Math.round(p.opportunity_score) : "—"}</span>
          </div>
          <div class="muted">${CIQ.esc(p.job_address || p.permit_number || "")} · ${CIQ.esc(p.jurisdiction || "")}</div>
          <div>${CIQ.esc(p.trade_scope || "—")}${p.location ? "" : ' · <span class="badge amber">Location not available</span>'}</div>
          <div class="mw-item-contact">${contactHtml(p.contact)}</div>
        </div>`).join("");
    }

    async function load() {
      $(".mw-viewall").href = "opportunities.html?" + params();
      $(".mw-summary").textContent = "Loading…";
      $(".mw-feed").innerHTML = CIQ.skeletonRows(4);
      let feed, layer;
      try {
        [feed, layer] = await Promise.all([
          CIQ.api.get("/api/sales/opportunities?" + params({ page_size: FEED_SIZE })),
          CIQ.api.get("/api/sales/opportunities/map?" + params()),
        ]);
      } catch (e) {
        $(".mw-feed").innerHTML = CIQ.errorBanner(e.message || "Could not load opportunities.");
        $(".mw-summary").textContent = "";
        return;
      }
      state.points = layer.points || [];
      state.today = o.todaysAccounts || [];
      const unplacedBits = Object.entries(layer.unplaced_by_jurisdiction || {})
        .map(([j, n]) => `${n.toLocaleString("en-US")} ${j.replace("_az", "").replace(/_/g, " ")}`).join(", ");
      const fmt = (n) => Number(n || 0).toLocaleString("en-US");
      $(".mw-summary").textContent = `${fmt(layer.total)} opportunities · `
        + `${fmt(layer.placed)} on the map · ${fmt(layer.unplaced)} without a published location`
        + (layer.not_loaded ? ` · ${fmt(layer.not_loaded)} beyond the top ${fmt(layer.scan_limit)} by score not mapped` : "")
        + (layer.scan_limit && !state.scope ? ` · checked the top ${fmt(layer.scan_limit)} of ${fmt(layer.candidates)} candidates by score` : "")
        + (state.scope ? " · all scopes" : " · non-wet and off-focus hidden");
      const todayUnplaced = state.today.filter((a) => !(a.project && a.project.location)).map((a) => a.display_name);
      $(".mw-foot").textContent = [layer.location_note,
        unplacedBits ? `Not placed: ${unplacedBits}.` : "",
        todayUnplaced.length ? `Today's accounts without a location: ${todayUnplaced.join(", ")}.` : ""].filter(Boolean).join(" ");
      $(".mw-feed").innerHTML = feedHtml(feed);
      $(".mw-feed").querySelectorAll(".mw-item").forEach((el) => el.addEventListener("click", () => {
        const pid = Number(el.dataset.pid);
        const p = state.points.find((x) => x.project_id === pid);
        if (!p || !state.map) return;
        state.map.setView([p.lat, p.lon], Math.max(state.map.getZoom(), 17));
        setTimeout(() => { const m = state.markers[pid]; if (m) m.openPopup(); }, 250);
      }));
      if (state.map) {
        if (state.points.length) state.map.fitBounds(L.latLngBounds(state.points.map((p) => [p.lat, p.lon])).pad(0.05));
        else state.map.setView(PHOENIX, 10);
        draw();
      }
      if (o.onLoaded) o.onLoaded(feed, layer);
    }

    const api = {
      reload: load,
      update(next) {
        if (next.todaysAccounts) o.todaysAccounts = next.todaysAccounts;
        if (next.onLoaded) o.onLoaded = next.onLoaded;
        return load();
      },
    };
    root._mapWorkspace = api;
    load();
    return api;
  };
})();
