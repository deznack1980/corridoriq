/* Supplier RFQ inbox — procurement preview, viewed as "Sample Supplier A".
 * SAMPLE DATA ONLY: requests are fictional (demo/rfq-sample-data.js). Prices
 * typed here stay in this browser tab; "Submit" sends nothing to anyone and
 * nothing calls the API. */
(function () {
  const AS = "A"; // this preview views the inbox as Sample Supplier A
  let current = null;
  let draft = {}; // line -> { px, av, ld }
  let extras = { delivery: "", valid: "", notes: "" };

  function S() { return window.CIQ_SAMPLE; }
  function P() { return window.CIQP; }

  function statusFor(item) {
    const st = P().state();
    if (item.id === S().rfq.id && st.inboxQuoted) return "Quoted";
    if (item.id === S().rfq.id && st.inboxViewed) return "Viewed";
    return item.status;
  }

  function renderList() {
    const el = document.getElementById("ibList");
    el.innerHTML = S().inbox.map((it) => {
      const status = statusFor(it);
      const cls = status === "New" ? "blue" : status === "Quoted" ? "green" : "slate";
      return `<button type="button" class="inbox-item ${current === it.id ? "on" : ""}" data-id="${CIQ.esc(it.id)}">
        <div class="ii-top"><span class="ii-from">${CIQ.esc(it.from)}</span><span class="badge ${cls}">${CIQ.esc(status)}</span></div>
        <div class="ii-title">${CIQ.esc(it.title)}</div>
        <div class="ii-meta">${CIQ.esc(it.id)} · ${CIQ.esc(it.location)} · ${it.lines} lines · due ${CIQ.esc(P().date(it.due))}</div>
      </button>`;
    }).join("");
    el.querySelectorAll(".inbox-item").forEach((b) => b.addEventListener("click", () => open(b.dataset.id)));
  }

  function totals() {
    const lines = P().sampleBom();
    let sub = 0, quoted = 0;
    lines.forEach((l) => {
      const d = draft[l.line];
      const px = d && d.av !== "none" ? Number(d.px) : NaN;
      if (isFinite(px) && px > 0) { sub += px * (l.qty || 0); quoted += 1; }
    });
    const del = Number(extras.delivery) || 0;
    return { sub, del, total: sub + del, quoted, of: lines.length };
  }

  function renderTotals() {
    const t = totals();
    const el = document.getElementById("ibTotals");
    if (!el) return;
    el.innerHTML = `
      <div class="stat-line"><span>Lines priced</span><span>${t.quoted} of ${t.of}</span></div>
      <div class="stat-line"><span>Materials</span><span>${P().money(t.sub)}</span></div>
      <div class="stat-line"><span>Delivery</span><span>${t.del ? P().money(t.del) : "Included"}</span></div>
      <div class="stat-line" style="font-size:15px"><span>Quote total</span><span>${P().money(t.total)}</span></div>`;
    document.querySelectorAll("[data-ext]").forEach((cell) => {
      const line = Number(cell.dataset.ext);
      const l = P().sampleBom().find((x) => x.line === line);
      const d = draft[line];
      const px = d && d.av !== "none" ? Number(d.px) : NaN;
      cell.textContent = isFinite(px) && px > 0 ? P().money(px * (l.qty || 0)) : "—";
    });
  }

  function fillSample() {
    const q = S().quotes[AS];
    Object.entries(q.lines).forEach(([line, e]) => {
      draft[line] = { px: e[0] == null ? "" : String(e[0]), av: e[1] === "days" ? "days" : e[1], ld: e[2] != null ? String(e[2]) : "" };
    });
    extras = { delivery: String(q.delivery_fee), valid: q.valid_until, notes: q.note };
    renderDetail();
    CIQ.toast("Sample pricing filled for Sample Supplier A.", "info");
  }

  async function submit() {
    const t = totals();
    if (!t.quoted) { CIQ.toast("Price at least one line before submitting.", "error"); return; }
    const ok = await CIQ.confirm(
      `Demonstration only — this quote (${P().money(t.total)}) will not be sent to the contractor or saved. Continue?`,
      { confirmLabel: "Submit sample quote" });
    if (!ok) return;
    P().update({ inboxQuoted: true, inboxQuoteTotal: t.total });
    CIQ.toast("Sample quote submitted — nothing was sent.", "success");
    renderList(); renderDetail();
  }

  function renderDetail() {
    const el = document.getElementById("ibDetail");
    const item = S().inbox.find((x) => x.id === current);
    if (!item) {
      el.innerHTML = CIQ.emptyState({ icon: "✉", title: "Select a request", text: "Choose an RFQ from the inbox to review its materials and prepare a quote." });
      return;
    }
    const isMain = item.id === S().rfq.id;
    const head = `<div class="card" style="margin-bottom:16px">
      <div class="card-head"><div><h3>${CIQ.esc(item.title)}</h3>
        <div class="sub">${CIQ.esc(item.from)} · ${CIQ.esc(item.id)} · received ${CIQ.esc(item.received)}</div></div>
        <span class="badge ${statusFor(item) === "Quoted" ? "green" : "blue"}">${CIQ.esc(statusFor(item))}</span></div>
      <div class="card-body meta-row">
        <div><div class="eyebrow">Location</div><div style="font-weight:600">${CIQ.esc(item.location)}</div></div>
        <div><div class="eyebrow">Quotes due</div><div style="font-weight:600">${CIQ.esc(P().date(item.due))}</div></div>
        <div><div class="eyebrow">Needed by</div><div style="font-weight:600">${isMain ? CIQ.esc(P().date(S().rfq.need_by)) : "—"}</div></div>
        <div><div class="eyebrow">Fulfillment</div><div style="font-weight:600">${isMain ? CIQ.esc(S().rfq.delivery) : "—"}</div></div>
      </div></div>`;
    if (!isMain) {
      el.innerHTML = head + CIQ.emptyState({ icon: "☰", title: `${item.lines} line items`,
        text: "This preview includes full material detail for RFQ-SAMPLE-1042 only.",
        action: `<button class="btn btn-sm btn-primary" type="button" id="ibGoMain">Open RFQ-SAMPLE-1042</button>` });
      document.getElementById("ibGoMain").addEventListener("click", () => open(S().rfq.id));
      return;
    }
    const st = P().state();
    if (st.inboxQuoted) {
      el.innerHTML = head + `<div class="card"><div class="done-box">
        <div class="ok">✓</div><h3 style="font-size:17px">Sample quote submitted</h3>
        <p class="muted" style="max-width:460px;margin:6px auto 16px">${P().money(st.inboxQuoteTotal)} for ${CIQ.esc(item.from)}. In production the contractor would see this quote alongside others — and you would never see theirs. In this preview nothing was sent.</p>
        <div class="row" style="justify-content:center">
          <button class="btn btn-sm" type="button" id="ibEdit">Revise quote</button>
          <a class="btn btn-sm btn-primary" href="contractor-quotes.html">See the contractor's comparison</a>
        </div></div></div>`;
      document.getElementById("ibEdit").addEventListener("click", () => { P().update({ inboxQuoted: false }); renderList(); renderDetail(); });
      return;
    }
    const lines = P().sampleBom();
    const rows = lines.map((l) => {
      const d = draft[l.line] || { px: "", av: "stock", ld: "" };
      return `<tr>
        <td class="muted tnum">${l.line}</td>
        <td style="min-width:180px">${CIQ.esc(l.description)}<div class="muted" style="font-size:11.5px">${CIQ.esc(l.category)}${l.status === "review" ? ' · <span style="color:var(--amber)">contractor asks you to confirm</span>' : ""}</div></td>
        <td class="num">${l.qty == null ? "—" : l.qty} <span class="muted">${CIQ.esc(l.unit)}</span></td>
        <td class="px"><input type="number" min="0" step="0.01" placeholder="0.00" data-line="${l.line}" data-f="px" value="${CIQ.esc(d.px)}" aria-label="Unit price line ${l.line}" /></td>
        <td class="av"><select data-line="${l.line}" data-f="av" aria-label="Availability line ${l.line}">
          ${[["stock", "In stock"], ["days", "Lead time"], ["backorder", "Backorder"], ["none", "Can't supply"]]
            .map(([v, t]) => `<option value="${v}" ${d.av === v ? "selected" : ""}>${t}</option>`).join("")}</select></td>
        <td class="ld"><input type="number" min="0" step="1" placeholder="days" data-line="${l.line}" data-f="ld" value="${CIQ.esc(d.ld)}" aria-label="Lead days line ${l.line}" /></td>
        <td class="num" data-ext="${l.line}">—</td>
      </tr>`;
    }).join("");
    el.innerHTML = head + `<div class="card">
      <div class="card-head"><div><h3>Material requirements &amp; your quote</h3><div class="sub">Enter your unit price and availability for each line</div></div>
        <button class="btn btn-sm" type="button" id="ibFill">Fill with sample pricing</button></div>
      <div class="table-wrap" style="border:0;border-radius:0;box-shadow:none"><table class="tbl qt">
        <thead><tr><th>#</th><th>Item</th><th class="num">Qty</th><th>Unit price</th><th>Availability</th><th>Lead</th><th class="num">Extended</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="card-body" style="border-top:1px solid var(--border)">
        <div class="split">
          <div class="form-grid">
            <label class="fld">Delivery fee<input type="number" min="0" step="0.01" id="ibDelivery" placeholder="0.00 = included" value="${CIQ.esc(extras.delivery)}" /></label>
            <label class="fld">Quote valid until<input type="date" id="ibValid" value="${CIQ.esc(extras.valid)}" /></label>
            <label class="fld full">Notes to contractor<textarea id="ibNotes" placeholder="Substitutions, lead-time notes, delivery windows…">${CIQ.esc(extras.notes)}</textarea></label>
          </div>
          <div><div id="ibTotals"></div>
            <button class="btn btn-positive btn-block" type="button" id="ibSubmit" style="margin-top:12px">Submit quote</button>
            <div class="muted" style="font-size:11.5px;margin-top:8px;text-align:center">Preview only — nothing is sent.</div></div>
        </div>
      </div></div>`;
    el.querySelectorAll("[data-line]").forEach((inp) => inp.addEventListener("input", () => {
      const line = inp.dataset.line;
      draft[line] = Object.assign({ px: "", av: "stock", ld: "" }, draft[line], { [inp.dataset.f]: inp.value });
      renderTotals();
    }));
    document.getElementById("ibDelivery").addEventListener("input", (e) => { extras.delivery = e.target.value; renderTotals(); });
    document.getElementById("ibValid").addEventListener("change", (e) => { extras.valid = e.target.value; });
    document.getElementById("ibNotes").addEventListener("input", (e) => { extras.notes = e.target.value; });
    document.getElementById("ibFill").addEventListener("click", fillSample);
    document.getElementById("ibSubmit").addEventListener("click", submit);
    renderTotals();
  }

  function open(id) {
    current = id;
    if (id === S().rfq.id) P().update({ inboxViewed: true });
    renderList(); renderDetail();
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", {
      title: "RFQ inbox", subtitle: "Procurement preview · supplier side", active: "rfq-inbox.html", sample: true,
    });
    if (!user) return;
    document.getElementById("ibAs").textContent = "Procurement · viewing as " + P().supplier(AS).name;
    open(S().rfq.id);
  });
})();
