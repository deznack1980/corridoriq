/* Contractor dashboard: requests, supplier connection, account. */
(function () {
  "use strict";
  let me = null;
  let items = [];

  function statusChips(r) {
    if (!r.sent_to || !r.sent_to.length) return `<span class="chip">${P.th("status_draft")}</span>`;
    return r.sent_to.map((s) => {
      const v = { supplier: s.supplier_name };
      const sub = s.status === "ACKNOWLEDGED" ? ["ack", "share_ack"] : s.status === "VIEWED" ? ["sent", "share_viewed"] : ["new", "share_new"];
      return `<span class="chip sent">${P.th("status_sent", v)}</span><span class="chip ${sub[0]}">${P.th(sub[1], v)}</span>`;
    }).join(" ");
  }

  function render() {
    document.getElementById("hello").textContent = P.t("home_hello", { name: me.user.display_name });
    document.getElementById("barBusiness").textContent = me.contractor.business_name;
    const list = document.getElementById("requests");
    list.innerHTML = items.map((r) => `
      <li><a href="/contractor-request.html?id=${encodeURIComponent(r.request_id)}">
        <span class="r-t">${P.esc(r.title)}</span>
        <span class="r-m">${r.reference ? `<span>${P.esc(r.reference)}</span>` : ""}
          <span>${P.th("lines_count", { n: r.line_count })}</span><span>${P.esc(P.fmtDate(r.updated_at))}</span></span>
        <span class="r-m">${statusChips(r)}</span>
      </a></li>`).join("");
    document.getElementById("noRequests").hidden = items.length > 0;

    const conns = me.connections || [];
    document.getElementById("connections").innerHTML = conns.map((c) => `
      <li><div style="display:grid;gap:4px;padding:12px 4px"><span class="r-t">${P.esc(c.supplier_name)}</span>
      <span class="r-m">${P.th("connected_since")} · ${P.esc(P.fmtDate(c.since))}</span></div></li>`).join("");
    document.getElementById("noConnections").hidden = conns.length > 0;

    const c = me.contractor;
    document.getElementById("account").innerHTML = `
      <dt>${P.th("f_name")}</dt><dd>${P.esc(me.user.display_name)}</dd>
      <dt>${P.th("f_business")}</dt><dd>${P.esc(c.business_name)}</dd>
      <dt>${P.th("f_email")}</dt><dd>${P.esc(me.user.email)}</dd>
      <dt>${P.th("f_phone")}</dt><dd>${P.esc(c.phone || "—")}</dd>
      <dt>${P.th("email_verification")}</dt><dd>${P.th("email_verification_pending")}</dd>`;
    document.getElementById("editProfile").hidden = !c.is_owner;
    document.getElementById("pfBusiness").value = c.business_name;
    document.getElementById("pfPhone").value = c.phone || "";
  }

  async function init() {
    P.mountLangSwitch(true);
    me = await P.me();
    if (!me) { location.replace("/contractor-account.html?mode=signin"); return; }
    if (me.account_type !== "contractor") { location.replace("/supplier-inbox.html"); return; }
    P.signedIn = true;
    P.adoptServerLang(me.language);
    try { items = (await P.api("GET", "/api/pilot/contractor/requests")).items; } catch (e) { items = []; }
    document.getElementById("main").hidden = false;
    render();
    P.onLang = render;
    document.getElementById("signOut").addEventListener("click", P.signOut);
    document.getElementById("profileForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const msg = document.getElementById("profileMsg");
      try {
        me = await P.api("PATCH", "/api/pilot/contractor/profile", {
          business_name: document.getElementById("pfBusiness").value,
          phone: document.getElementById("pfPhone").value,
        });
        render();
        msg.className = "msg ok"; msg.textContent = P.t("profile_saved");
      } catch (err) {
        msg.className = "msg err"; msg.textContent = P.errText(err);
      }
      msg.hidden = false;
    });
  }
  document.addEventListener("DOMContentLoaded", init);
})();
