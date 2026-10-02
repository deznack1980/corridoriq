/* Contractor sign-up / sign-in. */
(function () {
  "use strict";
  let referral = null;
  let started = false;

  function show(err, el) {
    el.hidden = !err;
    el.textContent = err ? P.errText(err) : "";
  }

  function renderFor() {
    const p = document.getElementById("signupFor");
    p.hidden = !referral;
    if (referral) p.textContent = P.t("signup_for", { supplier: referral.supplier_name });
  }

  async function init() {
    P.mountLangSwitch(false);
    const me = await P.me();
    if (me) {
      location.replace(me.account_type === "supplier" ? "/supplier-inbox.html" : "/contractor-home.html");
      return;
    }
    const mode = P.qs("mode") === "signin" ? "signin" : "signup";
    document.getElementById("signupForm").hidden = mode !== "signup";
    document.getElementById("signinForm").hidden = mode !== "signin";
    const attr = P.attribution();
    if (attr.referral_code) {
      try { referral = await P.api("GET", "/api/pilot/referral/" + encodeURIComponent(attr.referral_code)); } catch (e) { referral = null; }
    }
    renderFor();
    P.onLang = renderFor;

    document.getElementById("signupForm").addEventListener("focusin", () => {
      if (!started) { started = true; P.event("signup_started", "contractor-account"); }
    });

    document.getElementById("signupForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("suSubmit");
      btn.disabled = true;
      show(null, document.getElementById("signupErr"));
      const a = P.attribution();
      try {
        await P.api("POST", "/api/pilot/auth/signup", {
          name: document.getElementById("suName").value,
          business_name: document.getElementById("suBusiness").value,
          email: document.getElementById("suEmail").value,
          phone: document.getElementById("suPhone").value,
          password: document.getElementById("suPassword").value,
          language: P.lang(),
          referral_code: referral ? referral.code : undefined,
          utm: a.utm || {},
          visit_id: P.visitId(),
        });
        location.href = "/contractor-home.html";
      } catch (err) {
        show(err, document.getElementById("signupErr"));
        btn.disabled = false;
      }
    });

    document.getElementById("signinForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("siSubmit");
      btn.disabled = true;
      show(null, document.getElementById("signinErr"));
      try {
        const res = await P.api("POST", "/api/pilot/auth/login", {
          email: document.getElementById("siEmail").value,
          password: document.getElementById("siPassword").value,
          visit_id: P.visitId(),
        });
        location.href = res.account_type === "supplier" ? "/supplier-inbox.html" : "/contractor-home.html";
      } catch (err) {
        show(err, document.getElementById("signinErr"));
        btn.disabled = false;
      }
    });
  }
  document.addEventListener("DOMContentLoaded", init);
})();
