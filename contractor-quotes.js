/* Quote comparison + supplier selection — procurement preview.
 * SAMPLE DATA ONLY: quotes are fictional (demo/rfq-sample-data.js). Selecting
 * a supplier records a flag in this browser tab; no order is placed, no
 * supplier is notified, and nothing calls the API. */
(function () {
  function ids() {
    const S = window.CIQ_SAMPLE, st = window.CIQP.state();
    return st.rfqSuppliers && st.rfqSuppliers.length ? st.rfqSuppliers : S.suppliers.map((s) => s.id);
  }

  function render() {
    const S = window.CIQ_SAMPLE, P = window.CIQP, st = P.state();
    const sids = ids();
    const sums = sids.map((id) => P.quoteSummary(id));
    const complete = sums.filter((s) => s.complete);
    const lowestComplete = complete.length ? complete.reduce((a, b) => (b.total < a.total ? b : a)) : null;
    const lowestAny = sums.reduce((a, b) => (b.total < a.total ? b : a));
    const fastest = sums.reduce((a, b) => (b.lead < a.lead ? b : a));
    const selected = st.selected && sids.includes(st.selected) ? st.selected : null;

    document.getElementById("qRfq").textContent = `${S.rfq.id} · ${st.rfqTitle || S.rfq.title}`;
    document.getElementById("qStepper").innerHTML = P.stepper(selected ? 7 : 5);

    const selEl = document.getElementById("qSelected");
    if (selected) {
      const s = P.supplier(selected), sum = sums.find((x) => x.sid === selected);
      selEl.innerHTML = `<div class="sel-banner">
        <div>✓ <b>${CIQ.esc(s.name)}</b> selected for ${CIQ.esc(S.rfq.id)} at ${P.money(sum.total)}.
          <span style="color:var(--text-2)">Sample only — no order was placed and no supplier was notified.</span></div>
        <button class="btn btn-sm" id="qChange" type="button">Change selection</button></div>`;
      document.getElementById("qChange").addEventListener("click", () => {
        P.update({ selected: null }); render();
      });
    } else selEl.innerHTML = "";

    document.getElementById("select").innerHTML = sums.map((sum) => {
      const s = P.supplier(sum.sid);
      const badges = [];
      if (lowestComplete && sum.sid === lowestComplete.sid) badges.push('<span class="badge green">Lowest complete quote</span>');
      if (sum.sid === lowestAny.sid && (!lowestComplete || lowestAny.sid !== lowestComplete.sid))
        badges.push('<span class="badge blue">Lowest total</span>');
      if (sum.sid === fastest.sid) badges.push('<span class="badge blue">Fastest</span>');
      if (sum.complete) badges.push('<span class="badge outline">All lines quoted</span>');
      if (sum.missing.length) badges.push(`<span class="badge red">${sum.missing.length} line${sum.missing.length === 1 ? "" : "s"} not quoted</span>`);
      if (sum.backorder) badges.push(`<span class="badge amber">${sum.backorder} backordered</span>`);
      const isSel = selected === sum.sid;
      return `<div class="card q-card ${isSel ? "is-sel" : ""}">
        <div class="card-head"><div><h3>${CIQ.esc(s.name)}</h3><div class="sub">${CIQ.esc(s.area)}</div></div>
          ${isSel ? '<span class="badge green">Selected</span>' : ""}</div>
        <div class="card-body">
          <div class="eyebrow">Quote total</div>
          <div class="q-total">${P.money(sum.total)}</div>
          <div class="q-badges">${badges.join("")}</div>
          <div class="stat-line"><span>Materials</span><span>${P.money(sum.subtotal)}</span></div>
          <div class="stat-line"><span>Delivery</span><span>${sum.delivery ? P.money(sum.delivery) : "Included"}</span></div>
          <div class="stat-line"><span>Longest lead time</span><span>${sum.lead} day${sum.lead === 1 ? "" : "s"}</span></div>
          <div class="stat-line"><span>Valid until</span><span>${CIQ.esc(P.date(sum.valid_until))}</span></div>
          <div class="stat-line"><span>Terms</span><span>${CIQ.esc(sum.terms)}</span></div>
          <div class="muted" style="font-size:12px;margin-top:10px;line-height:1.45">${CIQ.esc(sum.note)}</div>
        </div>
        <div class="card-foot">
          ${isSel ? `<button class="btn btn-block" type="button" disabled>Selected</button>`
            : `<button class="btn btn-positive btn-block" type="button" data-select="${sum.sid}">Select ${CIQ.esc(s.name)}</button>`}
        </div>
      </div>`;
    }).join("");
    document.querySelectorAll("[data-select]").forEach((b) => b.addEventListener("click", () => choose(b.dataset.select)));

    // Line-by-line table.
    const lines = P.sampleBom();
    const head = sids.map((id) => `<th class="sup ${selected === id ? "sel-col" : ""}">${CIQ.esc(P.supplier(id).name.replace("Sample ", ""))}</th>`).join("");
    const body = lines.map((l) => {
      const prices = sids.map((id) => {
        const e = S.quotes[id].lines[l.line];
        return e && e[0] != null && e[1] !== "none" ? e[0] : null;
      });
      const valid = prices.filter((x) => x != null);
      const best = valid.length > 1 ? Math.min(...valid) : null;
      const cells = sids.map((id, k) => {
        const e = S.quotes[id].lines[l.line];
        const a = P.availLabel(e);
        const px = prices[k];
        const cls = [px != null && px === best ? "best" : "", selected === id ? "sel-col" : ""].join(" ");
        if (px == null) return `<td class="sup ${cls}"><span class="muted">—</span><div><span class="badge ${a.k}">${a.t}</span></div></td>`;
        return `<td class="sup ${cls}"><div class="px">${P.money(px)}</div>
          <div class="ext">${P.money(px * (l.qty || 0))}</div>
          <div><span class="badge ${a.k}">${CIQ.esc(a.t)}</span></div>
          ${e[3] ? `<div class="ext" style="color:var(--amber)">${CIQ.esc(e[3])}</div>` : ""}</td>`;
      }).join("");
      return `<tr><td class="muted tnum">${l.line}</td>
        <td class="item">${CIQ.esc(l.description)}<div class="muted" style="font-size:11.5px">${CIQ.esc(l.category)}</div></td>
        <td class="num">${l.qty == null ? "—" : l.qty} <span class="muted">${CIQ.esc(l.unit)}</span></td>${cells}</tr>`;
    }).join("");
    const foot = (label, fn) => `<tr><td></td><td>${label}</td><td></td>${sums.map((s) =>
      `<td class="sup ${selected === s.sid ? "sel-col" : ""}">${fn(s)}</td>`).join("")}</tr>`;
    document.getElementById("qTable").innerHTML = `<div class="table-wrap"><table class="tbl cmp">
      <thead><tr><th>#</th><th>Item</th><th class="num">Qty</th>${head}</tr></thead>
      <tbody>${body}</tbody>
      <tfoot>
        ${foot("Materials", (s) => P.money(s.subtotal))}
        ${foot("Delivery", (s) => s.delivery ? P.money(s.delivery) : "Included")}
        ${foot("<b>Total</b>", (s) => `<b>${P.money(s.total)}</b>`)}
      </tfoot></table></div>
      <div class="muted" style="font-size:12px;margin-top:8px">Green unit price = lowest quoted for that line. All prices are sample data.</div>`;
  }

  async function choose(sid) {
    const P = window.CIQP, s = P.supplier(sid);
    const ok = await CIQ.confirm(
      `Select ${s.name}? Demonstration only — no order will be placed and no supplier will be notified.`,
      { confirmLabel: "Select supplier (sample)" });
    if (!ok) return;
    P.update({ selected: sid, selectedAt: new Date().toISOString() });
    CIQ.toast(`${s.name} selected (sample).`, "success");
    render();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", {
      title: "Compare quotes", subtitle: "Procurement preview", active: "contractor-quotes.html",
      portal: "contractor", sample: true,
    });
    if (!user) return;
    render();
  });
})();
