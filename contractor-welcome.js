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
    document.getElementById("pfName").value = user.display_name || "";
    document.getElementById("pfBiz").value = user.business_name || d.business_name || "";

    const banner = document.getElementById("verifyBanner");
    banner.hidden = !!d.email_verified;
    document.getElementById("resendBtn").addEventListener("click", () => {
      CIQAuth.resendVerification(user.email, document.getElementById("verifyMsg"));
    });

    const labels = {
      live_rfq: "Live RFQs",
      protected_intelligence: "Protected intelligence",
      private_quotations: "Private quotations",
    };
    document.getElementById("locked").innerHTML = (d.locked || []).map((item) =>
      `<div class="lock-card" style="margin-bottom:12px">
        <div class="lock">Locked</div>
        <h3>${esc(labels[item.key] || item.key.replace(/_/g, " "))}</h3>
        <p>Email verification required to activate this feature.</p>
      </div>`).join("");

    document.getElementById("signOut").addEventListener("click", async () => {
      await CIQAuth.post("/api/auth/logout", {});
      location.href = "login.html";
    });

    CIQAuth.bindForm(document.getElementById("profileForm"), async (fd) => {
      const { ok, data } = await CIQAuth.get && await fetch("/api/contractor/profile", {
        method: "PATCH", credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: fd.get("name"), business_name: fd.get("business_name") }),
      }).then(async (r) => ({ ok: r.ok, data: await r.json().catch(() => null) }));
      const el = document.getElementById("profileMsg");
      CIQAuth.setMsg(el, ok ? "Profile saved." : ((data && data.error) || "Could not save."), ok ? "ok" : "error");
    });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
