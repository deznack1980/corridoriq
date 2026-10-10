(function () {
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  async function boot() {
    const me = await CIQAuth.get("/api/auth/me");
    if (!me.ok || !me.data || !me.data.user) {
      location.href = "login.html?expired=1";
      return;
    }
    const user = me.data.user;
    if ((user.roles || []).includes("admin")) {
      location.href = user.default_landing_page || "admin-dashboard.html";
      return;
    }
    const dash = await CIQAuth.get("/api/contractor/dashboard");
    if (!dash.ok) {
      location.href = user.default_landing_page || "login.html";
      return;
    }
    const d = dash.data;
    document.getElementById("hello").textContent = "Welcome, " + (user.display_name || "contractor");
    document.getElementById("bizSub").textContent = d.business_name || "Contractor workspace";
    if (d.value) document.getElementById("lede").textContent = d.value;
    document.getElementById("pfName").value = user.display_name || "";
    document.getElementById("pfBiz").value = user.business_name || d.business_name || "";

    const banner = document.getElementById("verifyBanner");
    banner.hidden = !!d.email_verified;
    document.getElementById("resendBtn").addEventListener("click", () => {
      CIQAuth.resendVerification(user.email, document.getElementById("verifyMsg"));
    });

    const demo = d.workflow_demo || {};
    if (demo.project) document.getElementById("demoProject").textContent = demo.project;
    if (demo.note) document.getElementById("demoNote").textContent = demo.note;
    document.querySelector("#bomTable tbody").innerHTML = (demo.bom || []).map((row) =>
      `<tr><td>${esc(row.line)}</td><td>${esc(row.description)}</td><td>${esc(row.qty)} ${esc(row.unit)}</td></tr>`
    ).join("");
    document.querySelector("#quoteTable tbody").innerHTML = (demo.suppliers || []).map((row) =>
      `<tr><td>${esc(row.name)}</td><td>${esc(row.status)}</td><td>${esc(row.availability)}</td><td>${esc(row.lead)}</td></tr>`
    ).join("");

    const labels = {
      live_rfq: "Live RFQs",
      protected_intelligence: "Protected intelligence",
      private_quotations: "Private quotations",
    };
    document.getElementById("locked").innerHTML = (d.locked || []).map((item) =>
      `<div class="lock-card" style="margin-bottom:12px">
        <div class="lock">Locked until verification</div>
        <h3>${esc(labels[item.key] || item.key.replace(/_/g, " "))}</h3>
        <p>Email verification required to activate this feature.</p>
      </div>`).join("");

    document.getElementById("signOut").addEventListener("click", async () => {
      await CIQAuth.post("/api/auth/logout", {});
      location.href = "login.html";
    });

    CIQAuth.bindForm(document.getElementById("profileForm"), async (fd) => {
      const res = await fetch("/api/contractor/profile", {
        method: "PATCH", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: fd.get("name"), business_name: fd.get("business_name") }),
      });
      const data = await res.json().catch(() => null);
      const el = document.getElementById("profileMsg");
      CIQAuth.setMsg(el, res.ok ? "Profile saved." : ((data && data.error) || "Could not save."), res.ok ? "ok" : "error");
    });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
