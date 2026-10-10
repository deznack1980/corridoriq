/* User management — list, create, enable/disable (admins). */
(function () {
  let users = [];
  const ROLES = [["sales_representative", "Sales Rep"], ["sales_manager", "Sales Manager"],
    ["admin", "Administrator"], ["read_only", "Read Only"], ["fulfillment_user", "Fulfillment"]];
  const canDisable = () => CIQ.hasPerm("users.disable");

  function render() {
    const el = document.getElementById("list");
    el.innerHTML = `<div class="table-wrap"><table class="tbl responsive"><thead><tr>
      <th>Name</th><th>Email</th><th>Roles</th><th>Status</th><th>Last login</th>${canDisable() ? "<th></th>" : ""}</tr></thead>
      <tbody>${users.map((u) => `<tr>
        <td data-label="Name">${CIQ.esc(u.display_name || (u.first_name + " " + u.last_name))}</td>
        <td data-label="Email">${CIQ.esc(u.email)}</td>
        <td data-label="Roles">${(u.roles || []).map((r) => `<span class="badge slate">${CIQ.esc(CIQ.titleCase(r))}</span>`).join(" ")}</td>
        <td data-label="Status">${statusBadge(u)}</td>
        <td data-label="Last login">${u.last_login_at ? CIQ.relTime(u.last_login_at) : "Never"}</td>
        ${canDisable() ? `<td data-label="">${actionButtons(u)}</td>` : ""}
      </tr>`).join("")}</tbody></table></div>`;
    el.querySelectorAll("button[data-id]").forEach((b) => b.addEventListener("click", () => toggle(Number(b.dataset.id), b)));
    el.querySelectorAll("button[data-approve]").forEach((b) => b.addEventListener("click", () => approve(Number(b.dataset.approve), b)));
  }

  function statusBadge(u) {
    const state = u.account_state || (u.is_active ? "ACTIVE" : "SUSPENDED");
    const kind = u.account_kind && u.account_kind !== "employee" ? ` · ${CIQ.titleCase(u.account_kind)}` : "";
    const cls = state === "ACTIVE" && u.is_active ? "green" : state === "SUSPENDED" || !u.is_active ? "red" : "amber";
    return `<span class="badge ${cls}">${CIQ.esc(state)}${kind}</span>`;
  }

  function actionButtons(u) {
    const bits = [`<button class="btn btn-sm" data-id="${u.id}">${u.is_active ? "Disable" : "Enable"}</button>`];
    if (u.account_kind === "supplier" && u.account_state === "PENDING_SUPPLIER_APPROVAL" && CIQ.hasPerm("users.update")) {
      bits.unshift(`<button class="btn btn-sm btn-primary" data-approve="${u.id}">Approve supplier</button>`);
    }
    return bits.join(" ");
  }

  async function approve(id, btn) {
    if (!(await CIQ.confirm(`Approve supplier access for this account? Live RFQs and contacts will unlock.`, { confirmLabel: "Approve" }))) return;
    await CIQ.busy(btn, async () => {
      try {
        const updated = await CIQ.api.post(`/api/admin/users/${id}/approve-supplier`, {});
        const idx = users.findIndex((x) => x.id === id);
        if (idx >= 0) Object.assign(users[idx], updated);
        CIQ.toast("Supplier approved", "success"); render();
      } catch (e) { CIQ.toast(e.message, "error"); }
    });
  }

  async function toggle(id, btn) {
    const u = users.find((x) => x.id === id);
    if (u.is_active && !(await CIQ.confirm(`Disable ${u.display_name || u.email}? Their sessions end immediately.`, { danger: true, confirmLabel: "Disable" }))) return;
    await CIQ.busy(btn, async () => {
      try {
        const updated = await CIQ.api.patch(`/api/admin/users/${id}`, { is_active: !u.is_active });
        Object.assign(u, updated); CIQ.toast("User updated", "success"); render();
      } catch (e) { CIQ.toast(e.message, "error"); }
    });
  }

  function newUser() {
    const body = document.createElement("form");
    body.className = "form-grid";
    body.innerHTML = `<label class="fld">First name<input name="first_name" /></label>
      <label class="fld">Last name<input name="last_name" /></label>
      <label class="fld full">Email<input name="email" type="email" required /></label>
      <label class="fld">Phone<input name="phone" /></label>
      <label class="fld">Role<select name="role">${ROLES.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select></label>`;
    const foot = document.createElement("div");
    foot.innerHTML = `<button type="button" class="btn" data-a="cancel">Cancel</button>
      <button type="button" class="btn btn-primary" data-a="save">Create user</button>`;
    const m = CIQ.modal({ title: "New user", body, footer: foot });
    foot.querySelector('[data-a="cancel"]').addEventListener("click", m.close);
    foot.querySelector('[data-a="save"]').addEventListener("click", async (e) => {
      const fd = new FormData(body);
      if (!(fd.get("email") || "").trim()) { CIQ.toast("Email is required", "error"); return; }
      await CIQ.busy(e.target, async () => {
        try {
          const res = await CIQ.api.post("/api/admin/users", {
            email: fd.get("email").trim(), first_name: fd.get("first_name"), last_name: fd.get("last_name"),
            phone: fd.get("phone") || null, roles: [fd.get("role")] });
          m.close(); await load();
          if (res.temporary_password) {
            CIQ.modal({ title: "User created", width: 420,
              body: `<p>Share this temporary password securely. It will not be shown again.</p>
                <div class="card card-pad" style="text-align:center;font-size:20px;font-weight:700;letter-spacing:1px">${CIQ.esc(res.temporary_password)}</div>`,
              footer: `<button class="btn btn-primary" onclick="this.closest('.modal-backdrop').remove()">Done</button>` });
          } else { CIQ.toast("User created", "success"); }
        } catch (err) { CIQ.toast(err.message, "error"); }
      });
    });
  }

  async function load() {
    document.getElementById("list").innerHTML = CIQ.skeletonRows(4);
    try { users = (await CIQ.api.get("/api/admin/users")).items || []; }
    catch (e) { document.getElementById("list").innerHTML = CIQ.errorBanner(e.message); return; }
    render();
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard("users.view", { title: "Users", subtitle: "Employee accounts", active: "user-management.html" });
    if (!user) return;
    const btn = document.getElementById("newUserBtn");
    if (CIQ.hasPerm("users.create")) btn.addEventListener("click", newUser); else btn.style.display = "none";
    load();
  });
})();
