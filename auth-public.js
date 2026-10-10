/* Shared helpers for public registration, login extras, verification, and reset. */
(function () {
  const A = {};

  A.qs = (k) => new URLSearchParams(location.search).get(k);

  A.post = async function (path, payload) {
    const res = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify(payload || {}),
    });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { ok: res.ok, status: res.status, data };
  };

  A.get = async function (path) {
    const res = await fetch(path, { credentials: "same-origin" });
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    return { ok: res.ok, status: res.status, data };
  };

  A.goLanding = function (user) {
    const dest = (user && user.default_landing_page) || "/login.html";
    window.location.href = dest.startsWith("/") ? dest : ("/" + dest);
  };

  A.setMsg = function (el, text, kind) {
    if (!el) return;
    el.textContent = text || "";
    el.className = "auth-msg" + (kind ? " " + kind : "");
  };

  A.bindForm = function (form, submit) {
    if (!form) return;
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = form.querySelector("[type=submit]");
      if (btn) btn.disabled = true;
      try { await submit(new FormData(form), btn); }
      finally { if (btn) btn.disabled = false; }
    });
  };

  A.resendVerification = async function (email, msgEl) {
    const { data } = await A.post("/api/auth/resend-verification", { email: email || "" });
    A.setMsg(msgEl, (data && data.message) || "If this account needs verification, a new link is on its way.", "ok");
  };

  window.CIQAuth = A;
})();
