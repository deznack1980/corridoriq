/* Contractor dashboard — procurement preview. SAMPLE DATA ONLY: everything on
 * this screen comes from demo/rfq-sample-data.js; nothing calls the API. */
(function () {
  function render() {
    const S = window.CIQ_SAMPLE, P = window.CIQP, st = P.state();
    const bom = P.bom();
    const selected = st.selected ? P.supplier(st.selected) : null;
    const quoted = S.rfq_recipients.filter((r) => r.events.some((e) => e[0] === "Quoted")).length;

    document.getElementById("cdEyebrow").textContent = "Contractor workspace · " + S.contractor.city;
    document.getElementById("cdTitle").textContent = S.contractor.name;
    document.getElementById("cdSub").textContent =
      `Procurement for ${S.project.name}. Build a bill of materials, request quotes from participating suppliers, and choose with everything side by side.`;

    const review = bom.items.filter((i) => i.status === "review").length;
    const blocks = [
      { k: "Active RFQs", v: selected ? "0" : "1" },
      { k: "Quotes received", v: `${quoted} of ${S.rfq_recipients.length}` },
      { k: selected ? "Supplier selected" : "Awaiting your decision", v: selected ? selected.name.replace("Sample ", "") : "1", cls: selected ? "" : "is-warn" },
      { k: "BOM lines to review", v: String(review), cls: review ? "is-warn" : "" },
    ];
    document.getElementById("cdStrip").innerHTML = blocks.map((b) =>
      `<div class="dash-strip-item ${b.cls || ""}"><div class="dash-strip-k">${CIQ.esc(b.k)}</div>
        <div class="dash-strip-v">${CIQ.esc(b.v)}</div></div>`).join("");

    const step = selected ? 7 : st.rfqSubmitted ? 5 : st.bom ? 3 : 1;
    document.getElementById("cdStepper").innerHTML = P.stepper(step);

    const rows = S.rfq_recipients.map((r) => {
      const s = P.supplier(r.supplier);
      const last = r.events[r.events.length - 1];
      const sum = P.quoteSummary(r.supplier);
      return `<tr>
        <td data-label="Supplier"><b>${CIQ.esc(s.name)}</b><div class="muted" style="font-size:12px">${CIQ.esc(s.area)}</div></td>
        <td data-label="Status"><span class="badge ${last[0] === "Quoted" ? "green" : "blue"}">${CIQ.esc(last[0])}</span>
          <div class="muted" style="font-size:12px;margin-top:3px">${CIQ.esc(last[1])}</div></td>
        <td data-label="Lines quoted" class="num">${sum.quoted} / ${sum.of}</td>
        <td data-label="Quote total" class="num"><b>${P.money(sum.total)}</b></td>
      </tr>`;
    }).join("");
    document.getElementById("cdActive").innerHTML = `<div class="card">
      <div class="card-head">
        <div><h3>${CIQ.esc(S.rfq.title)}</h3>
          <div class="sub">${CIQ.esc(S.rfq.id)} · quotes due ${CIQ.esc(P.date(S.rfq.due))} · need by ${CIQ.esc(P.date(S.rfq.need_by))}</div></div>
        <span class="badge ${selected ? "green" : "amber"}">${selected ? "Supplier selected" : "Decision needed"}</span>
      </div>
      <div class="table-wrap" style="border:0;border-radius:0;box-shadow:none"><table class="tbl responsive">
        <thead><tr><th>Supplier</th><th>Status</th><th class="num">Lines quoted</th><th class="num">Quote total</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <div class="card-foot row" style="justify-content:space-between">
        <span class="muted" style="font-size:12.5px">${selected ? `Selected: <b>${CIQ.esc(selected.name)}</b> — no order was placed (sample).` : "All participating suppliers have responded."}</span>
        <a class="btn btn-sm btn-primary" href="contractor-quotes.html">Compare quotes</a>
      </div>
    </div>`;

    document.getElementById("cdHistory").innerHTML = `<div class="table-wrap"><table class="tbl responsive">
      <thead><tr><th>RFQ</th><th>Sent</th><th>Status</th><th>Outcome</th></tr></thead>
      <tbody>${S.rfq_history.map((h) => `<tr>
        <td data-label="RFQ"><b>${CIQ.esc(h.title)}</b><div class="muted" style="font-size:12px">${CIQ.esc(h.id)}</div></td>
        <td data-label="Sent">${CIQ.esc(P.date(h.sent))}</td>
        <td data-label="Status"><span class="badge ${h.status === "Awarded" ? "green" : "slate"}">${CIQ.esc(h.status)}</span></td>
        <td data-label="Outcome">${CIQ.esc(h.note)}</td></tr>`).join("")}</tbody></table></div>`;

    document.getElementById("cdStart").innerHTML = `<a class="dropzone" href="contractor-bom.html" style="display:block;text-decoration:none">
        <div class="dz-ico">⇪</div><h3>Upload or build a BOM</h3>
        <p>Drop a CSV from your estimating tool, or start from the sample BOM.</p></a>
      <div class="row" style="margin-top:10px">
        <a class="btn btn-sm" href="contractor-rfq.html">Create RFQ</a>
        <a class="btn btn-sm" href="contractor-rfqs.html">RFQ status</a>
        <button class="btn btn-sm btn-ghost" id="cdReset" type="button">Reset preview</button>
      </div>`;
    document.getElementById("cdReset").addEventListener("click", () => {
      P.reset(); CIQ.toast("Preview reset to the start of the sample story.", "info"); render();
    });

    document.getElementById("cdProject").innerHTML = `<div class="card card-pad">
      <div style="font-weight:650">${CIQ.esc(S.project.name)}</div>
      <div class="muted" style="font-size:12.5px;margin-bottom:8px">${CIQ.esc(S.project.location)}</div>
      <div class="stat-line"><span>Stage</span><span>${CIQ.esc(S.project.stage)}</span></div>
      <div class="stat-line"><span>Current BOM</span><span>${bom.items.length} lines</span></div>
      <div class="stat-line"><span>BOM source</span><span>${bom.source === "upload" ? "Your upload" : "Sample BOM"}</span></div>
      <div class="stat-line"><span>Participating suppliers</span><span>${S.suppliers.length}</span></div>
    </div>`;
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", {
      title: "Contractor dashboard", subtitle: "Procurement preview", active: "contractor-dashboard.html",
      portal: "contractor", sample: true,
    });
    if (!user) return;
    render();
  });
})();
