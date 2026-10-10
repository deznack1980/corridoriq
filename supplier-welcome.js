(function () {
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  }

  const STATE_COPY = {
    PENDING_EMAIL_VERIFICATION: "Confirm your email, then an administrator reviews the account before live access.",
    PENDING_SUPPLIER_APPROVAL: "Email is confirmed. Live RFQs, contacts, and quoting stay locked until an administrator approves this account.",
    ACTIVE: "This supplier account is active.",
    SUSPENDED: "This account is suspended.",
  };

  async function boot() {
    const me = await CIQAuth.get("/api/auth/me");
    if (!me.ok || !me.data || !me.data.user) {
      location.href = "login.html?expired=1";
      return;
    }
    const user = me.data.user;
    const dash = await CIQAuth.get("/api/supplier/dashboard");
    if (!dash.ok) {
      location.href = user.default_landing_page || "login.html";
      return;
    }
    const d = dash.data;
    document.getElementById("hello").textContent = "Welcome, " + (user.display_name || "supplier");
    document.getElementById("bizSub").textContent = d.business_name || "Supplier workspace";
    document.getElementById("stateLine").textContent = STATE_COPY[d.account_state] || STATE_COPY.PENDING_EMAIL_VERIFICATION;

    const live = d.email_verified && d.account_state === "ACTIVE";
    const banner = document.getElementById("verifyBanner");
    banner.hidden = live;
    document.getElementById("gateTitle").textContent = d.email_verified
      ? "Administrative approval required to activate this feature."
      : "Email verification required to activate this feature.";
    document.getElementById("gateBody").textContent = d.verification_message || "";
    document.getElementById("resendBtn").hidden = !!d.email_verified;
    document.getElementById("resendBtn").addEventListener("click", () => {
      CIQAuth.resendVerification(user.email, document.getElementById("verifyMsg"));
    });

    const labels = {
      live_rfqs: "Live contractor RFQs",
      quote_submission: "Quote submission",
      contractor_contacts: "Contractor contacts",
      commercial_data: "Commercial data",
    };
    document.getElementById("locked").innerHTML = (d.locked || []).map((item) =>
      `<div class="lock-card" style="margin-bottom:12px">
        <div class="lock">Locked</div>
        <h3>${esc(labels[item.key] || item.key.replace(/_/g, " "))}</h3>
        <p>${esc(item.message)}</p>
      </div>`).join("");

    document.getElementById("signOut").addEventListener("click", async () => {
      await CIQAuth.post("/api/auth/logout", {});
      location.href = "login.html";
    });
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
