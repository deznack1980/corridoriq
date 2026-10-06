/* Reports — friendly catalog with downloads (no filesystem paths shown). */
(function () {
  function fileForPrefixes(files, prefixes) {
    return files.find((f) => prefixes.some((p) => f.name.toLowerCase().startsWith(p)));
  }

  function render(data) {
    const files = data.files || [];
    document.getElementById("cards").innerHTML = (data.reports || []).map((r) => {
      const latest = fileForPrefixes(files, r.file_prefixes || []);
      return `<div class="company-card">
        <div class="cc-name" style="font-size:15px">${CIQ.esc(r.title)}</div>
        <div class="cc-reason">${CIQ.esc(r.description)}</div>
        <div class="cc-meta"><div><span>For</span>${CIQ.esc(r.audience)}</div>
          <div><span>Latest</span>${latest ? CIQ.fmtDate(latest.generated_at) : "Not generated"}${latest && latest.predates_latest_refresh ? ' <span class="badge amber" title="Generated before the latest data refresh">Older than latest data</span>' : ""}</div></div>
        <div class="cc-quick">${latest
          ? `<a class="btn btn-sm btn-primary" href="/api/reports/download?name=${encodeURIComponent(latest.name)}">Download ${CIQ.esc(latest.type)}</a>`
          : `<button class="btn btn-sm" disabled>No file yet</button>`}</div>
      </div>`;
    }).join("");

    const el = document.getElementById("files");
    if (!files.length) {
      el.innerHTML = CIQ.emptyState({ icon: "⊟", title: "No generated reports",
        text: "Reports appear here after they are generated." });
      return;
    }
    el.innerHTML = `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
      <th>Report</th><th>Type</th><th>Generated</th><th>Size</th><th></th></tr></thead>
      <tbody>${files.map((f) => `<tr>
        <td data-label="Report">${CIQ.esc(f.name)}</td>
        <td data-label="Type"><span class="badge slate">${CIQ.esc(f.type)}</span></td>
        <td data-label="Generated">${CIQ.fmtDateTime(f.generated_at)}${f.predates_latest_refresh ? ` <span class="badge amber" title="${CIQ.esc(f.freshness_note || "")}">Older than latest data</span>` : ""}</td>
        <td data-label="Size">${f.size_kb} KB</td>
        <td data-label=""><a class="btn btn-sm" href="/api/reports/download?name=${encodeURIComponent(f.name)}">Download</a></td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("reports.view", { title: "Reports", subtitle: "Detailed exports on demand", active: "reports.html" });
    if (!user) return;
    document.getElementById("cards").innerHTML = CIQ.skeletonRows(2);
    try { render(await CIQ.api.get("/api/reports/catalog")); }
    catch (e) { document.getElementById("cards").innerHTML = CIQ.errorBanner(e.message); }
  });
})();
