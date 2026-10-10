(function () {
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function load() {
    let data;
    try {
      const res = await fetch("/api/public/preview", { credentials: "same-origin" });
      data = await res.json();
      if (!res.ok) throw new Error("preview unavailable");
    } catch (e) {
      document.querySelector("#oppTable tbody").innerHTML =
        "<tr><td colspan=\"4\">Demonstration preview is unavailable from this address. Open this page from the CorridorIQ site.</td></tr>";
      return;
    }
    if (data.disclaimer) {
      document.getElementById("previewDisclaimer").textContent = data.disclaimer;
    }
    const rows = (data.opportunities || []).map((o) =>
      `<tr><td data-label="Example"><b>${esc(o.title)}</b><div class="preview-note">${esc(o.signal)}</div></td>
       <td data-label="Market">${esc(o.city)}</td>
       <td data-label="Trade">${esc(o.trade)}</td>
       <td data-label="Stage">${esc(o.stage)}</td></tr>`).join("");
    document.querySelector("#oppTable tbody").innerHTML = rows ||
      "<tr><td colspan=\"4\">No demonstration opportunities.</td></tr>";

    const steps = document.getElementById("contractorSteps");
    steps.innerHTML = (data.contractor_workflow || []).map((s) =>
      `<li><div><h4>${esc(s.title)}</h4><p>${esc(s.detail)}</p></div></li>`).join("");

    const benefits = document.getElementById("supplierBenefits");
    benefits.innerHTML = (data.supplier_benefits || []).map((b) =>
      `<li><div>${esc(b)}</div></li>`).join("");
  }

  document.addEventListener("DOMContentLoaded", load);
})();
