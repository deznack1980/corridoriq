/* Supplier billing — Founding Supply Partner subscription.
 * The browser never decides price, amount, customer or entitlement: it asks
 * the server to start Checkout / open the Customer Portal and follows the
 * Stripe-hosted URL the server returns. Paid status shown here always comes
 * from /api/billing/status, which reflects signature-verified Stripe webhooks,
 * never from having landed on the success URL. */
(function () {
  const CHECKOUT_PREFIX = "https://checkout.stripe.com/";
  const PORTAL_PREFIX = "https://billing.stripe.com/";
  const STATE_LABEL = {
    none: "Not subscribed",
    checkout_pending: "Checkout started — not confirmed",
    incomplete: "Payment not yet confirmed",
    incomplete_expired: "Checkout expired",
    trialing: "Active (trial)",
    active: "Active",
    past_due: "Payment past due",
    unpaid: "Unpaid",
    paused: "Paused",
    canceled: "Canceled",
    unknown: "Status unavailable",
  };
  const LIVE = ["incomplete", "trialing", "active", "past_due", "unpaid", "paused", "unknown"];

  function notice(html, kind) {
    document.getElementById("billingNotice").innerHTML =
      html ? `<div class="${kind === "error" ? "error-banner" : "info-banner"}" role="status">${html}</div>` : "";
  }

  function fmtDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    return isNaN(d) ? "—" : d.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" });
  }

  function render(s) {
    const e = CIQ.esc;
    const canSubscribe = s.can_manage && s.billable && s.available && !s.paid_access && !LIVE.includes(s.state);
    const canManage = s.can_manage && s.has_billing_account && s.available;
    const rows = [
      ["Plan", e(s.plan.name)],
      ["Price", e(s.plan.display_price)],
      ["Status", e(STATE_LABEL[s.state] || STATE_LABEL.unknown)],
      ["Supplier intelligence access", s.paid_access ? "Included" : "Not active"],
    ];
    if (s.current_period_end && LIVE.includes(s.state)) {
      rows.push([s.cancel_at_period_end ? "Ends" : "Renews", e(fmtDate(s.current_period_end))]);
    }
    let actions = "";
    if (canSubscribe) {
      actions += `<button class="btn btn-primary" id="subscribeBtn">Become a Founding Partner — ${e(s.plan.display_price)}</button>`;
    }
    if (canManage) {
      actions += `<button class="btn btn-ghost" id="portalBtn">Manage Billing</button>`;
    }
    let note = "";
    if (!s.billable) note = "This organization is not billed through CorridorIQ.";
    else if (!s.can_manage) note = "Ask your organization's manager to manage billing.";
    else if (!s.available) note = "Billing is temporarily unavailable. Please try again later.";
    if (s.mode === "test") note += (note ? " " : "") + "Stripe test mode — no real charges.";

    document.getElementById("billing").innerHTML = `
      <div class="card" style="max-width:640px">
        <div class="card-head"><h3>${e(s.plan.name)}</h3></div>
        <div class="card-body">
          <dl style="display:grid;grid-template-columns:auto 1fr;gap:8px 18px;margin:0">${rows.map(([k, v]) => `<dt class="muted">${k}</dt><dd style="margin:0;font-weight:600">${v}</dd>`).join("")}</dl>
          ${actions ? `<div style="display:flex;flex-wrap:wrap;gap:10px;margin-top:16px">${actions}</div>` : ""}
          ${note ? `<p class="muted" style="margin-top:12px">${e(note)}</p>` : ""}
          <p class="muted" style="margin-top:12px">Payments are processed by Stripe. CorridorIQ never sees or stores card details.</p>
        </div>
      </div>`;
    const sub = document.getElementById("subscribeBtn");
    if (sub) sub.addEventListener("click", () => go("/api/billing/checkout", CHECKOUT_PREFIX, sub));
    const portal = document.getElementById("portalBtn");
    if (portal) portal.addEventListener("click", () => go("/api/billing/portal", PORTAL_PREFIX, portal));
  }

  async function go(endpoint, prefix, btn) {
    btn.disabled = true;
    try {
      const res = await CIQ.api.post(endpoint, {});
      if (!res || typeof res.url !== "string" || !res.url.startsWith(prefix)) throw new Error("Unexpected response");
      location.assign(res.url);
    } catch (err) {
      btn.disabled = false;
      CIQ.toast(err.message || "Billing is temporarily unavailable.", "error");
    }
  }

  async function load() {
    try {
      const s = await CIQ.api.get("/api/billing/status");
      render(s);
      return s;
    } catch (err) {
      document.getElementById("billing").innerHTML = CIQ.errorBanner(err.message || "Billing is unavailable.");
      return null;
    }
  }

  async function confirmAfterCheckout() {
    notice("Your subscription is being confirmed. This page updates when Stripe confirms the payment — it can take a minute.");
    for (let i = 0; i < 20; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      const s = await load();
      if (s && s.paid_access) {
        notice("Thank you — your Founding Supply Partner subscription is confirmed.");
        return;
      }
    }
    notice("We have not received confirmation from Stripe yet. You will not be charged twice — check back shortly, or contact archie@corridoriq.pro.");
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const user = await CIQ.guard(null, { title: "Billing", subtitle: "CorridorIQ subscription", active: "billing.html" });
    if (!user) return;
    const result = CIQ.qs("checkout");
    if (result) history.replaceState(null, "", location.pathname);  // drop session_id from the address bar
    await load();
    if (result === "success") confirmAfterCheckout();
    else if (result === "canceled") notice("Checkout was canceled. You have not been charged.");
  });
})();
