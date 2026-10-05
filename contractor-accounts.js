/* Contractor accounts — CorridorIQ operators only (server enforces admin.system +
 * CorridorIQ's own organization). Creates a contractor organization and its owner
 * with a one-time temporary password (stored only as a hash; shown once here). */
(function () {
  let items = [];

  function render() {
    const e = CIQ.esc;
    if (!items.length) {
      document.getElementById("list").innerHTML = `<div class="card card-pad muted">No contractor accounts yet.</div>`;
      return;
    }
    document.getElementById("list").innerHTML = `
      <div class="table-wrap"><table class="tbl responsive"><thead><tr>
        <th>Company</th><th>Owner</th><th>ZIP</th><th>ROC</th><th>Billing</th><th>Requests</th>
      </tr></thead><tbody>${items.map((a) => `<tr>
        <td>${e(a.name)}</td>
        <td>${a.owners.map((o) => `${e(o.display_name || o.email)}<div class="muted">${e(o.email)}${o.must_change_password ? " · must set password" : ""}</div>`).join("") || "—"}</td>
        <td>${e(a.business_zip || "—")}</td>
        <td>${e(a.roc_license || "—")}</td>
        <td>${e(a.billing_state)}${a.entitlements.includes("contractor_pro") ? " · Contractor Pro" : ""}</td>
        <td>${a.request_priority === "priority" ? "Priority" : "Standard"}</td>
      </tr>`).join("")}</tbody></table></div>`;
  }

  function newAccount() {
    const body = document.createElement("form");
    body.innerHTML = `
      <label class="fld">Company name *<input name="company_name" required /></label>
      <label class="fld">Contact name *<input name="contact_name" required /></label>
      <label class="fld">Email *<input name="email" type="email" required /></label>
      <label class="fld">Business ZIP<input name="business_zip" inputmode="numeric" /></label>
      <label class="fld">Business address (if no ZIP)<input name="business_address" /></label>
      <label class="fld">Arizona ROC license (optional)<input name="roc_license" /></label>
      <label class="fld">Phone (optional)<input name="phone" /></label>`;
    const foot = document.createElement("div");
    foot.innerHTML = `<button type="button" class="btn" data-a="cancel">Cancel</button>
      <button type="button" class="btn btn-primary" data-a="save">Create account</button>`;
    const m = CIQ.modal({ title: "New contractor account", body, footer: foot });
    foot.querySelector('[data-a="cancel"]').addEventListener("click", m.close);
    foot.querySelector('[data-a="save"]').addEventListener("click", async (ev) => {
      const fd = new FormData(body);
      const payload = {};
      for (const k of ["company_name", "contact_name", "email", "business_zip", "business_address", "roc_license", "phone"]) {
        const v = (fd.get(k) || "").trim();
        if (v) payload[k] = v;
      }
      await CIQ.busy(ev.target, async () => {
        try {
          const res = await CIQ.api.post("/api/admin/contractor-accounts", payload);
          m.close(); await load();
          CIQ.modal({ title: "Contractor account created", width: 440,
            body: `<p>Give this temporary password to ${CIQ.esc(res.owner.email)} through a secure channel.
              It will not be shown again, and they must choose a new password at first sign-in.</p>
              <div class="card card-pad" style="text-align:center;font-size:20px;font-weight:700;letter-spacing:1px">${CIQ.esc(res.temporary_password)}</div>`,
            footer: `<button class="btn btn-primary" onclick="this.closest('.modal-backdrop').remove()">Done</button>` });
        } catch (err) {
          const fields = err.data && err.data.fields;
          CIQ.toast(fields ? Object.entries(fields).map(([k, v]) => `${k} ${v}`).join("; ") : err.message, "error", 6000);
        }
      });
    });
  }

  async function load() {
    document.getElementById("list").innerHTML = CIQ.skeletonRows(3);
    try { items = (await CIQ.api.get("/api/admin/contractor-accounts")).items || []; }
    catch (e) { document.getElementById("list").innerHTML = CIQ.errorBanner(e.message); return; }
    render();
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("admin.system", { title: "Contractor accounts", subtitle: "Contractor Pro customers",
                                                   active: "contractor-accounts.html" });
    if (!user) return;
    document.getElementById("newContractorBtn").addEventListener("click", newAccount);
    load();
  });
})();
