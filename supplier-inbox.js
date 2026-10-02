/* Supplier inbox: requests explicitly sent to this supplier (English only). */
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let items = [];
  let current = null;
  const LABEL = { NEW: ["new", "New"], VIEWED: ["sent", "Viewed"], ACKNOWLEDGED: ["ack", "Acknowledged"] };
  const when = (iso) => { try { return new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); } catch (e) { return iso; } };
  const chip = (s) => `<span class="chip ${LABEL[s][0]}">${LABEL[s][1]}</span>`;

  function renderList() {
    $("list").innerHTML = items.map((r) => `
      <li><button class="row" type="button" data-id="${P.esc(r.share_id)}">
        <span class="r-t">${P.esc(r.contractor_name)} — ${P.esc(r.title)}</span>
        <span class="r-m">${r.reference ? `<span>${P.esc(r.reference)}</span>` : ""}<span>${r.line_count} lines</span>
          <span>${P.esc(when(r.sent_at))}</span>${chip(r.status)}</span>
      </button></li>`).join("");
    $("empty").hidden = items.length > 0;
    document.querySelectorAll("#list [data-id]").forEach((b) => b.addEventListener("click", () => open(b.dataset.id)));
  }

  function renderDetail() {
    const d = current;
    $("detail").hidden = !d;
    if (!d) return;
    $("dMeta").textContent = `${d.contractor_name} · received ${when(d.sent_at)}`;
    $("dTitle").textContent = d.title;
    $("dRef").textContent = d.reference || "";
    $("dStatus").innerHTML = chip(d.status) + (d.acknowledged_at ? ` <span class="muted">${P.esc(when(d.acknowledged_at))}</span>` : "");
    $("dLines").innerHTML = d.lines.map((l) => `<tr><td>${l.line_no}</td><td>${P.esc(l.raw_description)}</td>
      <td class="q">${P.esc(P.fmtQty(l.quantity))}</td><td>${P.esc(l.uom || "")}</td><td>${P.esc(l.note || "")}</td></tr>`).join("");
    $("ack").hidden = d.status === "ACKNOWLEDGED";
  }

  async function load() {
    items = (await P.api("GET", "/api/pilot/supplier/requests")).items;
    renderList();
  }

  async function open(id) {
    try {
      current = await P.api("GET", "/api/pilot/supplier/requests/" + encodeURIComponent(id));
      renderDetail();
      await load();
      if (window.innerWidth < 760) $("detail").scrollIntoView({ behavior: "smooth" });
    } catch (e) { $("err").hidden = false; $("err").textContent = "Could not open that request."; }
  }

  async function start(me) {
    $("signin").hidden = true;
    $("inbox").hidden = false;
    $("signOut").hidden = false;
    $("supplierName").textContent = me.supplier.name;
    await load();
  }

  async function init() {
    const me = await P.me();
    if (me && me.account_type === "contractor") { location.replace("/contractor-home.html"); return; }
    if (me) await start(me); else $("signin").hidden = false;
    $("signin").addEventListener("submit", async (e) => {
      e.preventDefault();
      try {
        const res = await P.api("POST", "/api/pilot/auth/login", { email: $("email").value, password: $("password").value });
        if (res.account_type !== "supplier") { location.replace("/contractor-home.html"); return; }
        await start(res);
      } catch (err) {
        $("signinErr").hidden = false;
        $("signinErr").textContent = err.code === "too_many_requests" ? "Too many attempts. Please wait a few minutes." : "Invalid email or password.";
      }
    });
    $("ack").addEventListener("click", async () => {
      current = await P.api("POST", `/api/pilot/supplier/requests/${current.share_id}/acknowledge`, {});
      renderDetail();
      await load();
    });
    $("signOut").addEventListener("click", async () => {
      try { await P.api("POST", "/api/pilot/auth/logout", {}); } catch (e) { /* ignore */ }
      location.reload();
    });
  }
  document.addEventListener("DOMContentLoaded", init);
})();
