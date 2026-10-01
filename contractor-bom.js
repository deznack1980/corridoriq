/* BOM create / upload / review — procurement preview. SAMPLE DATA ONLY: an
 * uploaded CSV is parsed in the browser and kept in this tab; nothing is sent
 * to the API or to any supplier. */
(function () {
  let bom = null; // { source, name, items }

  function S() { return window.CIQ_SAMPLE; }
  function P() { return window.CIQP; }

  function save() {
    P().update({ bom: bom && bom.items.length ? bom : null });
  }

  function renumber() { bom.items.forEach((it, i) => { it.line = i + 1; }); }

  function renormalize(i) {
    const it = bom.items[i];
    const [fresh] = P().normalize([{ line: it.line, description: it.description, qty: it.qty, unit: it.unit }]);
    if (it.accepted && fresh.qty) { fresh.issues = []; fresh.status = "ok"; fresh.accepted = true; }
    bom.items[i] = fresh;
  }

  function renderSummary() {
    const el = document.getElementById("bomSummary");
    if (!bom || !bom.items.length) {
      el.innerHTML = `<div class="muted" style="font-size:13px">No BOM yet. Upload a CSV or use the sample BOM.</div>`;
      document.getElementById("bomContinue").disabled = true;
      return;
    }
    const review = bom.items.filter((i) => i.status === "review").length;
    const cats = new Set(bom.items.map((i) => i.category)).size;
    el.innerHTML = `
      <div class="stat-line"><span>Lines</span><span>${bom.items.length}</span></div>
      <div class="stat-line"><span>Ready</span><span style="color:var(--signal-strong)">${bom.items.length - review}</span></div>
      <div class="stat-line"><span>Needs review</span><span style="color:${review ? "var(--amber)" : "inherit"}">${review}</span></div>
      <div class="stat-line"><span>Material categories</span><span>${cats}</span></div>
      <div class="progress" style="margin-top:12px"><span style="width:${Math.round((bom.items.length - review) / bom.items.length * 100)}%;background:var(--signal)"></span></div>
      <div class="muted" style="font-size:12px;margin-top:10px">${review ? "Lines that still need review are sent with a note asking suppliers to confirm." : "Every line is ready to quote."}</div>`;
    document.getElementById("bomContinue").disabled = false;
  }

  function renderTable() {
    const el = document.getElementById("bomTable");
    document.getElementById("bomStepper").innerHTML = P().stepper(bom && bom.items.length ? 2 : 1);
    document.getElementById("bomSource").textContent = bom
      ? (bom.source === "upload" ? `Loaded from ${bom.name}` : "Sample BOM loaded") : "";
    if (!bom || !bom.items.length) {
      el.innerHTML = CIQ.emptyState({ icon: "☰", title: "No BOM yet",
        text: "Upload a CSV from your estimating tool, or load the sample BOM to see the review step.",
        action: `<button class="btn btn-sm btn-primary" type="button" data-a="sample">Use sample BOM</button>` });
      el.querySelector('[data-a="sample"]').addEventListener("click", useSample);
      renderSummary();
      return;
    }
    el.innerHTML = `<div class="table-wrap"><table class="tbl bom-tbl">
      <thead><tr><th>#</th><th>Description</th><th>Matched category</th><th class="num">Qty</th><th>Unit</th><th>Status</th><th></th></tr></thead>
      <tbody>${bom.items.map((it, i) => `<tr class="${it.status === "review" ? "is-review" : ""}">
        <td class="muted tnum">${it.line}</td>
        <td class="desc"><input data-i="${i}" data-f="description" value="${CIQ.esc(it.description)}" aria-label="Description line ${it.line}" />
          ${it.issues.length ? `<ul class="issues">${it.issues.map((x) => `<li>${CIQ.esc(x)}</li>`).join("")}</ul>` : ""}</td>
        <td><span class="cat ${it.category === "Needs a category" ? "none" : ""}">${CIQ.esc(it.category)}</span></td>
        <td class="qty num"><input data-i="${i}" data-f="qty" type="number" min="0" step="any" value="${it.qty == null ? "" : it.qty}" aria-label="Quantity line ${it.line}" /></td>
        <td><input data-i="${i}" data-f="unit" value="${CIQ.esc(it.unit)}" style="width:60px" aria-label="Unit line ${it.line}" /></td>
        <td class="st">${it.status === "review"
          ? `<span class="badge amber">Needs review</span><div><button class="btn btn-sm btn-ghost" data-accept="${i}" type="button" style="margin-top:4px;padding:0 6px">Accept as is</button></div>`
          : `<span class="badge green">Ready</span>`}</td>
        <td><button class="btn btn-sm btn-ghost" data-del="${i}" type="button" title="Remove line" aria-label="Remove line ${it.line}">✕</button></td>
      </tr>`).join("")}</tbody></table></div>`;
    el.querySelectorAll("input[data-f]").forEach((inp) => inp.addEventListener("change", () => {
      const i = Number(inp.dataset.i), f = inp.dataset.f;
      bom.items[i][f] = f === "qty" ? inp.value : inp.value.trim();
      renormalize(i); save(); renderTable();
    }));
    el.querySelectorAll("[data-accept]").forEach((b) => b.addEventListener("click", () => {
      const it = bom.items[Number(b.dataset.accept)];
      if (!it.qty) { CIQ.toast("Add a quantity before accepting this line.", "error"); return; }
      it.accepted = true; it.issues = []; it.status = "ok"; save(); renderTable();
    }));
    el.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", () => {
      bom.items.splice(Number(b.dataset.del), 1); renumber(); save(); renderTable();
    }));
    renderSummary();
  }

  function useSample() {
    bom = { source: "sample", name: "Sample BOM", items: P().sampleBom() };
    save(); renderTable();
    CIQ.toast("Sample BOM loaded — 14 lines to review.", "info");
  }

  function loadFile(file) {
    if (!file) return;
    if (!/\.csv$/i.test(file.name)) { CIQ.toast("Please choose a .csv file.", "error"); return; }
    if (file.size > 2 * 1024 * 1024) { CIQ.toast("Preview limit is 2 MB.", "error"); return; }
    const reader = new FileReader();
    reader.onload = () => {
      const res = P().parseCSV(String(reader.result || ""));
      if (res.error) { CIQ.toast(res.error, "error"); return; }
      bom = { source: "upload", name: file.name, items: P().normalize(res.items) };
      save(); renderTable();
      const review = bom.items.filter((i) => i.status === "review").length;
      CIQ.toast(`${bom.items.length} lines read · ${review} need review`, review ? "info" : "success");
    };
    reader.onerror = () => CIQ.toast("Could not read that file.", "error");
    reader.readAsText(file);
  }

  function downloadTemplate() {
    const csv = "description,qty,unit\nPEX-A tubing 3/4in x 300ft coil,4,coil\nBall valve lead-free brass 3/4in sweat,18,ea\n";
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const a = document.createElement("a");
    a.href = url; a.download = "corridoriq-bom-template.csv";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", {
      title: "Bill of materials", subtitle: "Procurement preview", active: "contractor-bom.html",
      portal: "contractor", sample: true,
    });
    if (!user) return;
    const st = P().state();
    bom = st.bom && st.bom.items && st.bom.items.length ? st.bom : null;
    document.getElementById("bomProject").textContent = "Bill of materials · " + S().project.name;
    document.getElementById("bomStepper").innerHTML = P().stepper(bom ? 2 : 1);

    const drop = document.getElementById("bomDrop");
    document.getElementById("bomFile").addEventListener("change", (e) => loadFile(e.target.files[0]));
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("drag"); }));
    drop.addEventListener("drop", (e) => loadFile(e.dataTransfer.files[0]));
    document.getElementById("bomSample").addEventListener("click", useSample);
    document.getElementById("bomAdd").addEventListener("click", () => {
      if (!bom) bom = { source: "upload", name: "manual entry", items: [] };
      bom.items.push(...P().normalize([{ line: bom.items.length + 1, description: "", qty: "", unit: "ea" }]));
      save(); renderTable();
      const inputs = document.querySelectorAll('#bomTable input[data-f="description"]');
      if (inputs.length) inputs[inputs.length - 1].focus();
    });
    document.getElementById("bomTemplate").addEventListener("click", downloadTemplate);
    document.getElementById("bomClear").addEventListener("click", () => { bom = null; save(); renderTable(); });
    document.getElementById("bomContinue").addEventListener("click", () => {
      if (!bom || !bom.items.length) return;
      save(); location.href = "contractor-rfq.html";
    });
    renderTable();
  });
})();
