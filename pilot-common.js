/* CorridorIQ contractor pilot — shared client helpers.
 * Talks only to /api/pilot/*. The session lives in an HTTP-only cookie the
 * page never sees. Browser storage holds only: language choice, a random
 * per-visit ID (sessionStorage), and the referral code + UTM values from the
 * link the visitor arrived on — no personal data. */
(function () {
  "use strict";
  const I18N = window.PILOT_I18N;
  const LANG_KEY = "ciq_pilot_lang";
  const ATTR_KEY = "ciq_pilot_attr";
  const VISIT_KEY = "ciq_pilot_visit";
  const UTM_KEYS = ["utm_source", "utm_medium", "utm_campaign", "utm_content"];

  function store(kind) { try { return window[kind]; } catch (e) { return null; } }
  function get(kind, key) { try { const s = store(kind); return s ? s.getItem(key) : null; } catch (e) { return null; } }
  function put(kind, key, val) { try { const s = store(kind); if (s) s.setItem(key, val); } catch (e) { /* ignore */ } }

  const P = (window.P = {});

  P.esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---- language -----------------------------------------------------------
  P.lang = function () {
    const v = get("localStorage", LANG_KEY);
    if (v === "en" || v === "es") return v;
    return (navigator.language || "").toLowerCase().startsWith("es") ? "es" : "en";
  };
  P.t = function (key, vars) {
    const table = I18N[P.lang()] || I18N.en;
    let s = table[key] != null ? table[key] : (I18N.en[key] != null ? I18N.en[key] : key);
    return s.replace(/\{(\w+)\}/g, (m, k) => (vars && vars[k] != null ? String(vars[k]) : m));
  };
  /** Translated text with {vars} HTML-escaped. */
  P.th = function (key, vars) {
    const safe = {};
    Object.keys(vars || {}).forEach((k) => { safe[k] = P.esc(vars[k]); });
    return P.t(key, safe);
  };
  P.applyI18n = function (root) {
    (root || document).querySelectorAll("[data-i18n]").forEach((el) => {
      el.textContent = P.t(el.getAttribute("data-i18n"));
    });
    (root || document).querySelectorAll("[data-i18n-ph]").forEach((el) => {
      el.setAttribute("placeholder", P.t(el.getAttribute("data-i18n-ph")));
    });
    document.documentElement.lang = P.lang();
    document.querySelectorAll(".lang-switch button").forEach((b) => {
      b.setAttribute("aria-pressed", b.dataset.lang === P.lang() ? "true" : "false");
    });
  };
  P.setLang = async function (lang, signedIn) {
    if (lang !== "en" && lang !== "es") return;
    put("localStorage", LANG_KEY, lang);
    if (signedIn) {
      try { await P.api("PATCH", "/api/pilot/me/language", { language: lang }); } catch (e) { /* keep local */ }
    }
    P.applyI18n();
    if (typeof P.onLang === "function") P.onLang();
  };
  P.mountLangSwitch = function (signedIn) {
    document.querySelectorAll(".lang-switch").forEach((box) => {
      box.innerHTML = '<button type="button" data-lang="en">EN</button><button type="button" data-lang="es">ES</button>';
      box.setAttribute("role", "group");
      box.setAttribute("aria-label", "Language / Idioma");
      box.querySelectorAll("button").forEach((b) =>
        b.addEventListener("click", () => P.setLang(b.dataset.lang, P.signedIn != null ? P.signedIn : signedIn)));
    });
    P.applyI18n();
  };
  /** Server preference wins once signed in. */
  P.adoptServerLang = function (lang) {
    if ((lang === "en" || lang === "es") && lang !== get("localStorage", LANG_KEY)) {
      put("localStorage", LANG_KEY, lang);
    }
    P.applyI18n();
  };

  // ---- visit + attribution --------------------------------------------------
  P.visitId = function () {
    let v = get("sessionStorage", VISIT_KEY);
    if (!v) {
      const a = new Uint8Array(12);
      (window.crypto || {}).getRandomValues ? window.crypto.getRandomValues(a) : a.forEach((_, i) => (a[i] = Math.random() * 256));
      v = "v-" + Array.from(a, (b) => b.toString(16).padStart(2, "0")).join("");
      put("sessionStorage", VISIT_KEY, v);
    }
    return v;
  };
  P.captureAttribution = function (referralCode) {
    const qs = new URLSearchParams(location.search);
    const utm = {};
    UTM_KEYS.forEach((k) => { const v = qs.get(k); if (v) utm[k] = v.slice(0, 80); });
    const prev = P.attribution();
    const next = {
      referral_code: referralCode || prev.referral_code || null,
      utm: Object.keys(utm).length ? utm : (referralCode && referralCode !== prev.referral_code ? {} : prev.utm || {}),
    };
    put("localStorage", ATTR_KEY, JSON.stringify(next));
    return next;
  };
  P.attribution = function () {
    try { return JSON.parse(get("localStorage", ATTR_KEY) || "{}") || {}; } catch (e) { return {}; }
  };
  P.event = function (event, page) {
    const a = P.attribution();
    P.api("POST", "/api/pilot/events", {
      event, page, visit_id: P.visitId(), referral_code: a.referral_code || undefined, utm: a.utm || {},
    }).catch(() => {});
  };

  // ---- API -----------------------------------------------------------------
  P.api = async function (method, path, body) {
    const opts = { method, credentials: "same-origin", headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(path, opts);
    let data = {};
    try { data = await res.json(); } catch (e) { data = {}; }
    if (!res.ok) {
      const err = new Error(data.error || "generic");
      err.code = data.error || "generic";
      err.status = res.status;
      throw err;
    }
    return data;
  };
  P.errText = function (err) {
    const key = "err_" + ((err && err.code) || "generic");
    const t = P.t(key);
    return t === key ? P.t("err_generic") : t;
  };
  P.me = async function () {
    try {
      const s = await P.api("GET", "/api/pilot/session");
      return s.signed_in ? s : null;
    } catch (e) { return null; }
  };
  P.signOut = async function () {
    try { await P.api("POST", "/api/pilot/auth/logout", {}); } catch (e) { /* ignore */ }
    location.href = "/contractor-account.html?mode=signin";
  };
  P.qs = (k) => new URLSearchParams(location.search).get(k);
  P.fmtDate = function (iso) {
    if (!iso) return "";
    try {
      return new Date(iso).toLocaleString(P.lang() === "es" ? "es-MX" : "en-US",
        { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
    } catch (e) { return iso; }
  };
  P.fmtQty = (q) => (Number.isInteger(q) ? String(q) : String(Math.round(q * 10000) / 10000));
})();
