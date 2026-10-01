/* RFQ status — procurement preview. SAMPLE DATA ONLY: supplier status events
 * and quotes are fictional (demo/rfq-sample-data.js). Nothing calls the API. */
(function () {
  function render() {
    const S = window.CIQ_SAMPLE, P = window.CIQP, st = P.state();
    const ids = st.rfqSuppliers && st.rfqSuppliers.length ? st.rfqSuppliers : S.suppliers.map((s) => s.id);
    const recipients = S.rfq_recipients.filter((r) => ids.includes(r.supplier));
    const selected = st.selected || null;
    const title = st.rfqTitle || S.rfq.title;

    document.getElementById("rfqsStepper").innerHTML = P.stepper(selected ? 7 : 4);
    document.getElementById("rfqsNote").innerHTML = st.rfqSubmitted ? "" :
      `<div class="info-banner">Showing the sample RFQ. <a href="contractor-bom.html">Start from a BOM</a> to walk through the full story.</div>`;

    const cards = recipients.map((r) => {
      const s = P.supplier(r.supplier), sum = P.quoteSummary(r.supplier);
      const isSel = selected === r.supplier;
      return `<div class="card" style="${isSel ? "border-color:var(--signal);box-shadow:var(--ring)" : ""}">
        <div class="card-head"><div><h3>${CIQ.esc(s.name)}</h3><div class="sub">${CIQ.esc(s.area)}</div></div>
          ${isSel ? '<span class="badge green">Selected</span>' : '<span class="badge green">Quoted</span>'}</div>
        <div class="card-body">
          <div class="timeline">${r.events.map((e) => `<div class="tl-item done">
            <div class="tl-head"><span class="tl-type">${CIQ.esc(e[0])}</span><span class="tl-when">${CIQ.esc(e[1])}</span></div></div>`).join("")}
          </div>
          <div class="stat-line"><span>Quote total</span><span>${P.money(sum.total)}</span></div>
          <div class="stat-line"><span>Lines quoted</span><span>${sum.quoted} of ${sum.of}</span></div>
          <div class="stat-line"><span>Valid until</span><span>${CIQ.esc(P.date(sum.valid_until))}</span></div>
        </div>
      </div>`;
    }).join("");

    document.getElementById("rfqsActive").innerHTML = `<div class="card" style="margin-bottom:16px">
        <div class="card-head">
          <div><h3>${CIQ.esc(title)}</h3>
            <div class="sub">${CIQ.esc(S.rfq.id)} · ${CIQ.esc(S.project.name)}</div></div>
          <span class="badge ${selected ? "green" : "blue"}">${selected ? "Supplier selected" : CIQ.esc(S.rfq.status)}</span>
        </div>
        <div class="card-body meta-row">
          <div><div class="eyebrow">Sent</div><div style="font-weight:600">${CIQ.esc(P.date(S.rfq.created))}</div></div>
          <div><div class="eyebrow">Quotes due</div><div style="font-weight:600">${CIQ.esc(P.date(S.rfq.due))}</div></div>
          <div><div class="eyebrow">Needed by</div><div style="font-weight:600">${CIQ.esc(P.date(S.rfq.need_by))}</div></div>
          <div><div class="eyebrow">Responses</div><div style="font-weight:600">${recipients.length} of ${recipients.length} quoted</div></div>
        </div>
        <div class="card-foot row" style="justify-content:flex-end">
          <a class="btn btn-sm btn-primary" href="contractor-quotes.html">${selected ? "View selection" : "Compare quotes →"}</a>
        </div>
      </div>
      <div class="grid-3">${cards}</div>`;

    document.getElementById("rfqsHistory").innerHTML = `<div class="table-wrap"><table class="tbl responsive">
      <thead><tr><th>RFQ</th><th>Sent</th><th>Status</th><th>Outcome</th></tr></thead>
      <tbody>${S.rfq_history.map((h) => `<tr>
        <td data-label="RFQ"><b>${CIQ.esc(h.title)}</b><div class="muted" style="font-size:12px">${CIQ.esc(h.id)}</div></td>
        <td data-label="Sent">${CIQ.esc(P.date(h.sent))}</td>
        <td data-label="Status"><span class="badge ${h.status === "Awarded" ? "green" : "slate"}">${CIQ.esc(h.status)}</span></td>
        <td data-label="Outcome">${CIQ.esc(h.note)}</td></tr>`).join("")}</tbody></table></div>`;
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", {
      title: "RFQs & status", subtitle: "Procurement preview", active: "contractor-rfqs.html",
      portal: "contractor", sample: true,
    });
    if (!user) return;
    render();
  });
})();
