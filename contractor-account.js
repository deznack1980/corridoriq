/* Contractor sign-up / sign-in. Email-first: no session until the address is proven. */
(function () {
  "use strict";
  let referral = null;
  let started = false;

  function show(err, el) {
    el.hidden = !err;
    el.textContent = err ? P.errText(err) : "";
  }

  function fragmentToken() {
    const h = location.hash || "";
    const m = h.match(/(?:^#|&)t=([^&]+)/);
    return m ? decodeURIComponent(m[1]) : "";
  }

  function modeOf() {
    const m = P.qs("mode");
    if (["signin", "complete", "verify", "forgot", "reset"].includes(m)) return m;
    return "signup";
  }

  function renderFor() {
    const p = document.getElementById("signupFor");
    if (!p) return;
    p.hidden = !referral;
    if (referral) p.textContent = P.t("signup_for", { supplier: referral.supplier_name });
  }

  function hideAll() {
    ["signupForm", "signinForm", "completeForm", "verifyForm", "forgotForm", "resetForm"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.hidden = true;
    });
  }

  async function init() {
    P.mountLangSwitch(false);
    const mode = modeOf();
    if (mode !== "complete" && mode !== "verify" && mode !== "reset") {
      const me = await P.me();
      if (me) {
        location.replace(me.account_type === "supplier" ? "/supplier-inbox.html" : "/contractor-home.html");
        return;
      }
    }
    hideAll();
    const formId = mode + "Form";
    const shown = document.getElementById(formId);
    if (shown) shown.hidden = false;
    const attr = P.attribution();
    if (attr.referral_code) {
      try { referral = await P.api("GET", "/api/pilot/referral/" + encodeURIComponent(attr.referral_code)); } catch (e) { referral = null; }
    }
    renderFor();
    P.onLang = renderFor;

    const signupForm = document.getElementById("signupForm");
    signupForm.addEventListener("focusin", () => {
      if (!started) { started = true; P.event("signup_started", "contractor-account"); }
    });
    signupForm.addEventListener("submit", async (e) => {
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
          language: P.lang(),
          referral_code: referral ? referral.code : undefined,
          utm: a.utm || {},
          visit_id: P.visitId(),
        });
        const ok = document.getElementById("signupOk");
        ok.hidden = false;
        ok.textContent = P.t("check_email_lede");
        signupForm.querySelectorAll("input,button").forEach((el) => { el.disabled = true; });
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

    document.getElementById("completeForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("coSubmit");
      btn.disabled = true;
      show(null, document.getElementById("completeErr"));
      try {
        const res = await P.api("POST", "/api/pilot/auth/complete-signup", {
          token: fragmentToken(),
          password: document.getElementById("coPassword").value,
        });
        location.href = res.account_type === "supplier" ? "/supplier-inbox.html" : "/contractor-home.html";
      } catch (err) {
        show(err, document.getElementById("completeErr"));
        btn.disabled = false;
      }
    });

    document.getElementById("verifyForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("veSubmit");
      btn.disabled = true;
      show(null, document.getElementById("verifyErr"));
      try {
        await P.api("POST", "/api/pilot/auth/verify-email", { token: fragmentToken() });
        const ok = document.getElementById("verifyOk");
        ok.hidden = false;
        ok.textContent = P.t("email_verification_verified");
        setTimeout(() => { location.href = "/contractor-home.html"; }, 800);
      } catch (err) {
        show(err, document.getElementById("verifyErr"));
        btn.disabled = false;
      }
    });

    document.getElementById("forgotForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("foSubmit");
      btn.disabled = true;
      show(null, document.getElementById("forgotErr"));
      try {
        await P.api("POST", "/api/pilot/auth/forgot", { email: document.getElementById("foEmail").value });
        const ok = document.getElementById("forgotOk");
        ok.hidden = false;
        ok.textContent = P.t("check_email_lede");
      } catch (err) {
        show(err, document.getElementById("forgotErr"));
        btn.disabled = false;
      }
    });

    document.getElementById("resetForm").addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = document.getElementById("reSubmit");
      btn.disabled = true;
      show(null, document.getElementById("resetErr"));
      try {
        await P.api("POST", "/api/pilot/auth/reset", {
          token: fragmentToken(),
          password: document.getElementById("rePassword").value,
        });
        const ok = document.getElementById("resetOk");
        ok.hidden = false;
        ok.textContent = P.t("reset_done");
        setTimeout(() => { location.href = "/contractor-account.html?mode=signin"; }, 1200);
      } catch (err) {
        show(err, document.getElementById("resetErr"));
        btn.disabled = false;
      }
    });
  }
  document.addEventListener("DOMContentLoaded", init);
})();
