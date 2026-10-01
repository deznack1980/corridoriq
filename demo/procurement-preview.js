/* ==========================================================================
   Procurement preview — shared front-end logic (SAMPLE DATA ONLY)
   --------------------------------------------------------------------------
   Powers the contractor BOM → RFQ → quote → selection preview and the
   supplier RFQ inbox preview. State lives only in this browser tab
   (sessionStorage). This file never calls the CorridorIQ API, never sends
   anything to a supplier, and never places an order.
   ========================================================================== */
(function () {
  const S = window.CIQ_SAMPLE;
  const KEY = "ciq.sample.procurement.v1";
  const P = {};

  /* ---- Tab-local state (cleared when the tab closes) ---------------- */
  function read() {
    try { return JSON.parse(window.sessionStorage.getItem(KEY) || "{}") || {}; } catch (e) { return {}; }
  }
  function write(st) {
    try { window.sessionStorage.setItem(KEY, JSON.stringify(st)); } catch (e) { /* preview still works */ }
  }
  P.state = read;
  P.update = function (patch) { const st = Object.assign(read(), patch); write(st); return st; };
  P.reset = function () { try { window.sessionStorage.removeItem(KEY); } catch (e) { /* ignore */ } };

  /* ---- Formatting ---------------------------------------------------- */
  P.money = (v) => v == null || !isFinite(v) ? "—"
    : "$" + Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  P.money0 = (v) => v == null || !isFinite(v) ? "—"
    : "$" + Math.round(Number(v)).toLocaleString("en-US");
  P.date = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso + "T12:00:00");
    return isNaN(d) ? iso : d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  };
  P.supplier = (id) => S.suppliers.find((s) => s.id === id) || { id, name: "Sample supplier " + id };

  /* ---- BOM normalization (rule-based, reviewable) -------------------- */
  const RULES = [
    [/\bpex\b/i, "PEX tubing"],
    [/copper/i, "Copper pipe"],
    [/ball valve/i, "Ball valve"],
    [/water heater/i, "Water heater"],
    [/backflow/i, "Backflow preventer"],
    [/expansion tank/i, "Expansion tank"],
    [/\bdwv\b|\bpvc\b|\babs\b/i, "DWV pipe & fittings"],
    [/lavatory|\blav\b|sink/i, "Lavatory / sink"],
    [/water closet|toilet|urinal/i, "Water closet / urinal"],
    [/hanger|strap|clamp|unistrut/i, "Hangers & supports"],
    [/fitting|coupling|tee\b|elbow/i, "Fittings"],
  ];
  const UNITS = { coil: "coil", coils: "coil", ea: "ea", each: "ea", pc: "ea", pcs: "ea", piece: "ea",
    ft: "ft", feet: "ft", lf: "ft", lot: "lot", box: "box", bx: "box", case: "case" };

  P.normalize = function (rows) {
    return (rows || []).map((r, i) => {
      const desc = String(r.description || "").trim();
      const rule = RULES.find(([re]) => re.test(desc));
      const u = String(r.unit || "").trim().toLowerCase();
      const qty = Number(String(r.qty == null ? "" : r.qty).replace(/,/g, ""));
      const issues = [];
      if (!desc) issues.push("Description is empty");
      if (/per plan|see sheet|misc\b|tbd/i.test(desc)) issues.push("Refers to drawings — list the actual items");
      else if (!rule) issues.push("No standard category matched");
      if (!isFinite(qty) || qty <= 0) issues.push("Quantity missing");
      if (u && !UNITS[u]) issues.push(`Unit “${r.unit}” not recognized`);
      return {
        line: r.line || i + 1,
        description: desc,
        category: rule ? rule[1] : "Needs a category",
        qty: isFinite(qty) && qty > 0 ? qty : null,
        unit: UNITS[u] || u || "ea",
        issues,
        status: issues.length ? "review" : "ok",
      };
    });
  };

  /* ---- CSV upload (parsed in the browser; nothing is uploaded) ------- */
  P.parseCSV = function (text) {
    const rows = [];
    let row = [], cell = "", q = false;
    for (let i = 0; i < text.length; i++) {
      const ch = text[i];
      if (q) {
        if (ch === '"' && text[i + 1] === '"') { cell += '"'; i++; }
        else if (ch === '"') q = false;
        else cell += ch;
      } else if (ch === '"') q = true;
      else if (ch === ",") { row.push(cell); cell = ""; }
      else if (ch === "\n" || ch === "\r") {
        if (ch === "\r" && text[i + 1] === "\n") i++;
        row.push(cell); cell = "";
        if (row.some((c) => c.trim() !== "")) rows.push(row);
        row = [];
      } else cell += ch;
    }
    row.push(cell);
    if (row.some((c) => c.trim() !== "")) rows.push(row);
    if (!rows.length) return { items: [], error: "The file is empty." };

    const head = rows[0].map((h) => h.trim().toLowerCase());
    const find = (...names) => head.findIndex((h) => names.some((n) => h === n || h.includes(n)));
    let di = find("description", "item", "material", "product");
    let qi = find("qty", "quantity", "count");
    let ui = find("unit", "uom");
    let body = rows.slice(1);
    if (di < 0 && qi < 0) { di = 0; qi = 1; ui = 2; body = rows; } // headerless: desc, qty, unit
    if (di < 0) return { items: [], error: "Could not find a description column. Use columns: description, qty, unit." };
    const items = body.map((r, i) => ({
      line: i + 1,
      description: (r[di] || "").trim(),
      qty: qi >= 0 ? (r[qi] || "").trim() : "",
      unit: ui >= 0 ? (r[ui] || "").trim() : "",
    })).filter((r) => r.description || r.qty);
    if (!items.length) return { items: [], error: "No line items found." };
    if (items.length > 500) return { items: [], error: "Preview limit is 500 lines." };
    return { items, error: null };
  };

  /* ---- Current BOM for the story ------------------------------------- */
  P.sampleBom = () => P.normalize(S.bom_raw);
  P.bom = function () {
    const st = read();
    if (st.bom && st.bom.items && st.bom.items.length) return st.bom;
    return { source: "sample", name: "Sample BOM — " + S.project.name, items: P.sampleBom() };
  };

  /* ---- Sample quote math --------------------------------------------- */
  // Totals for one supplier against the sample RFQ's lines.
  P.quoteSummary = function (sid) {
    const q = S.quotes[sid];
    const lines = P.sampleBom();
    let subtotal = 0, quoted = 0, backorder = 0, maxLead = q.lead_days || 0;
    const missing = [];
    lines.forEach((l) => {
      const entry = q.lines[l.line];
      if (!entry || entry[0] == null || entry[1] === "none") { missing.push(l.line); return; }
      quoted += 1;
      subtotal += entry[0] * (l.qty || 0);
      if (entry[1] === "backorder") backorder += 1;
      if (entry[2] != null) maxLead = Math.max(maxLead, entry[2]);
    });
    return {
      sid, subtotal, delivery: q.delivery_fee, total: subtotal + (q.delivery_fee || 0),
      quoted, of: lines.length, missing, backorder, complete: !missing.length,
      lead: maxLead, valid_until: q.valid_until, terms: q.terms, note: q.note,
    };
  };
  P.availLabel = function (entry) {
    if (!entry || entry[1] === "none" || entry[0] == null) return { t: "Not quoted", k: "red" };
    if (entry[1] === "stock") return { t: "In stock", k: "green" };
    if (entry[1] === "backorder") return { t: `Backorder · ${entry[2]}d`, k: "amber" };
    return { t: `${entry[2]} day${entry[2] === 1 ? "" : "s"}`, k: "blue" };
  };

  /* ---- Workflow stepper ---------------------------------------------- */
  const STEPS = [
    ["contractor-bom.html", "Upload BOM"],
    ["contractor-bom.html#review", "Review items"],
    ["contractor-rfq.html", "Create RFQ"],
    ["contractor-rfqs.html", "RFQ status"],
    ["contractor-quotes.html", "Compare quotes"],
    ["contractor-quotes.html#select", "Select supplier"],
  ];
  P.stepper = function (current) {
    return `<nav class="stepper" aria-label="Procurement steps">${STEPS.map(([href, label], i) => {
      const n = i + 1;
      const cls = n < current ? "done" : n === current ? "current" : "";
      return `<a class="step ${cls}" href="${href}"><span class="n">${n < current ? "✓" : n}</span><span>${label}</span></a>`;
    }).join("")}</nav>`;
  };

  window.CIQP = P;
})();
