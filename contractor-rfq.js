/* Create RFQ — procurement preview. SAMPLE DATA ONLY: "Send" records a flag
 * in this browser tab and moves to the status screen. No RFQ is created, no
 * supplier receives anything, and nothing calls the API. */
(function () {
  const chosen = new Set();
  let showAll = false;

  function summary() {
    const P = window.CIQP, bom = P.bom();
    const review = bom.items.filter((i) => i.status === "review").length;
    document.getElementById("rfqSummary").innerHTML = `
      <div class="stat-line"><span>Line items</span><span>${bom.items.length}</span></div>
      <div class="stat-line"><span>Flagged for supplier review</span><span>${review}</span></div>
      <div class="stat-line"><span>Suppliers</span><span>${chosen.size}</span></div>
      <div class="stat-line"><span>Quotes due</span><span>${CIQ.esc(P.date(document.getElementById("rfqDue").value))}</span></div>
      <div class="stat-line"><span>Needed by</span><span>${CIQ.esc(P.date(document.getElementById("rfqNeed").value))}</span></div>`;
    document.getElementById("rfqSend").disabled = !chosen.size || !bom.items.length;
  }

  function renderSuppliers() {
    const S = window.CIQ_SAMPLE;
    const el = document.getElementById("rfqSuppliers");
    el.innerHTML = S.suppliers.map((s) => `<label class="check-card ${chosen.has(s.id) ? "on" : ""}">
        <input type="checkbox" data-sid="${s.id}" ${chosen.has(s.id) ? "checked" : ""} />
        <div style="min-width:0">
          <div style="font-weight:650">${CIQ.esc(s.name)}</div>
          <div class="muted" style="font-size:12px">${CIQ.esc(s.area)}</div>
          <div class="muted" style="font-size:12px;margin-top:2px">${CIQ.esc(s.response)}</div>
          <span class="badge green" style="margin-top:8px">Accepts RFQs</span>
        </div></label>`).join("");
    el.querySelectorAll("input[data-sid]").forEach((cb) => cb.addEventListener("change", () => {
      if (cb.checked) chosen.add(cb.dataset.sid); else chosen.delete(cb.dataset.sid);
      renderSuppliers(); summary();
    }));
  }

  function renderItems() {
    const bom = window.CIQP.bom();
    const items = showAll ? bom.items : bom.items.slice(0, 8);
    const review = bom.items.filter((i) => i.status === "review").length;
    document.getElementById("rfqItemsSub").textContent =
      `${bom.items.length} lines from ${bom.source === "upload" ? bom.name : "the sample BOM"}${review ? ` · ${review} flagged for supplier review` : ""}`;
    document.getElementById("rfqItems").innerHTML = `<div class="table-wrap" style="border:0;border-radius:0;box-shadow:none"><table class="tbl responsive">
      <thead><tr><th>#</th><th>Item</th><th>Category</th><th class="num">Qty</th><th>Unit</th></tr></thead>
      <tbody>${items.map((it) => `<tr>
        <td data-label="#" class="muted tnum">${it.line}</td>
        <td data-label="Item">${CIQ.esc(it.description || "—")}${it.status === "review" ? ' <span class="badge amber">Confirm</span>' : ""}</td>
        <td data-label="Category">${CIQ.esc(it.category)}</td>
        <td data-label="Qty" class="num">${it.qty == null ? "—" : it.qty}</td>
        <td data-label="Unit">${CIQ.esc(it.unit)}</td></tr>`).join("")}</tbody></table></div>
      ${bom.items.length > 8 ? `<div class="card-foot"><button class="btn btn-sm btn-ghost" id="rfqMore" type="button">${showAll ? "Show fewer" : `Show all ${bom.items.length} lines`}</button></div>` : ""}`;
    const more = document.getElementById("rfqMore");
    if (more) more.addEventListener("click", () => { showAll = !showAll; renderItems(); });
  }

  async function send() {
    const P = window.CIQP;
    const names = Array.from(chosen).map((id) => P.supplier(id).name).join(", ");
    const ok = await CIQ.confirm(
      `Demonstration only — no RFQ will be created and ${names} will not receive anything. Continue to the sample RFQ status?`,
      { confirmLabel: "Continue" });
    if (!ok) return;
    P.update({
      rfqSubmitted: new Date().toISOString(),
      rfqSuppliers: Array.from(chosen),
      rfqTitle: document.getElementById("rfqTitle").value.trim(),
    });
    location.href = "contractor-rfqs.html";
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", {
      title: "Create RFQ", subtitle: "Procurement preview", active: "contractor-rfq.html",
      portal: "contractor", sample: true,
    });
    if (!user) return;
    const S = window.CIQ_SAMPLE, P = window.CIQP, st = P.state(), bom = P.bom();
    (st.rfqSuppliers || S.suppliers.map((s) => s.id)).forEach((id) => chosen.add(id));
    document.getElementById("rfqProject").textContent = "Request for quote · " + S.project.name;
    document.getElementById("rfqStepper").innerHTML = P.stepper(3);
    document.getElementById("rfqTitle").value = st.rfqTitle || S.rfq.title;
    document.getElementById("rfqProj").value = S.project.name;
    document.getElementById("rfqLoc").value = S.project.location;
    document.getElementById("rfqDue").value = S.rfq.due;
    document.getElementById("rfqNeed").value = S.rfq.need_by;
    if (bom.source === "upload") {
      document.getElementById("rfqNote").innerHTML = `<div class="info-banner">You're using your uploaded BOM. In this preview, supplier quotes are shown for the sample BOM.</div>`;
    }
    ["rfqDue", "rfqNeed"].forEach((id) => document.getElementById(id).addEventListener("change", summary));
    document.getElementById("rfqSend").addEventListener("click", send);
    renderSuppliers(); renderItems(); summary();
  });
})();
