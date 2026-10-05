/* Contractor join page: /join/<referral code> or /contractor-join.html. */
(function () {
  "use strict";
  const m = location.pathname.match(/^\/join\/([a-z0-9-]{4,40})\/?$/);
  const code = m ? m[1] : null;
  let referral = null;
  let me = null;

  function render() {
    const name = referral ? referral.supplier_name : null;
    const v = { supplier: name };
    document.getElementById("joinKicker").hidden = !referral;
    document.getElementById("joinTitle").textContent = referral ? P.t("join_title", v) : P.t("join_generic_title");
    document.getElementById("joinLede").textContent = referral ? P.t("join_lede", v) : P.t("join_generic_lede");
    document.getElementById("step1").textContent = P.t("join_step1");
    document.getElementById("step2").textContent = P.t("join_step2");
    document.getElementById("step3").textContent = referral ? P.t("join_step3", v) : P.t("join_step3_generic");
    const note = document.getElementById("joinNote");
    note.hidden = !referral;
    if (referral) note.textContent = P.t("join_note", v);
    const inv = document.getElementById("joinInvalid");
    inv.hidden = !(code && !referral);
    inv.textContent = P.t("join_invalid");

    const contractor = me && me.account_type === "contractor";
    document.getElementById("anonActions").hidden = !!contractor;
    document.getElementById("userActions").hidden = !contractor;
    if (contractor && referral) {
      const connected = (me.connections || []).some((c) => c.supplier_name === referral.supplier_name);
      const btn = document.getElementById("connectBtn");
      btn.hidden = connected;
      btn.textContent = P.t("join_connect", v);
      const ok = document.getElementById("connectedMsg");
      ok.hidden = !connected;
      ok.textContent = P.t("join_connected", v);
    }
  }

  async function init() {
    P.mountLangSwitch(false);
    if (code) {
      try { referral = await P.api("GET", "/api/pilot/referral/" + encodeURIComponent(code)); } catch (e) { referral = null; }
    }
    P.captureAttribution(referral ? referral.code : null);
    me = await P.me();
    P.signedIn = !!me;
    if (me) P.adoptServerLang(me.language);
    render();
    P.onLang = render;
    P.event(referral ? "supplier_referral_view" : "landing_view", "contractor-join");
    document.getElementById("connectBtn").addEventListener("click", async () => {
      try {
        const res = await P.api("POST", "/api/pilot/contractor/connections", { referral_code: referral.code });
        me.connections = res.connections;
        render();
      } catch (e) { alert(P.errText(e)); }
    });
  }
  document.addEventListener("DOMContentLoaded", init);
})();
