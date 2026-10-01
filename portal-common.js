/* CorridorIQ Sales Workspace — shared client runtime.
 * Renders the app shell (sidebar + topbar), navigation gated by permission,
 * and reusable components (toasts, modals, the Log Activity form, badges,
 * pagination, date helpers). Security note: hiding a link is never a
 * substitute for authorization — the backend enforces every permission and
 * record-level access rule. The session lives in an HTTP-only cookie only. */
(function () {
  const CIQ = {};
  CIQ.user = null;
  let authed = false;

  /* ---- API ---------------------------------------------------------- */
  async function request(method, path, body) {
    const opts = { method, credentials: "include", headers: { "Content-Type": "application/json" } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    let res;
    try {
      res = await fetch(path, opts);
    } catch (e) {
      throw new Error("Network error — check your connection.");
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (res.status === 401 && authed && !location.pathname.endsWith("login.html")) {
      location.href = "login.html?expired=1";
      throw new Error("Session expired");
    }
    if (!res.ok) {
      const err = new Error((data && data.error) || res.statusText || "Request failed");
      err.status = res.status; err.data = data;
      throw err;
    }
    return data;
  }
  CIQ.api = {
    get: (p) => request("GET", p),
    post: (p, b) => request("POST", p, b),
    patch: (p, b) => request("PATCH", p, b),
  };

  CIQ.hasPerm = (key) => {
    if (!CIQ.user) return false;
    let perms = CIQ.user.permissions;
    if (typeof perms === "string") {
      try { perms = JSON.parse(perms); } catch (e) { perms = []; }
    }
    if (!Array.isArray(perms)) perms = perms ? Array.from(perms) : [];
    return perms.includes("admin.system") || perms.includes(key);
  };
  CIQ.logout = async function () {
    try { await CIQ.api.post("/api/auth/logout", {}); } catch (e) {}
    location.href = "login.html";
  };

  /* ---- Navigation config (role-aware) ------------------------------- */
  // Procurement preview: front-end-only sample screens (no API writes), shown
  // to administrators only until the RFQ backend exists.
  const PREVIEW_GROUP = { label: "Procurement preview", items: [
    { href: "rfq-inbox.html", label: "RFQ Inbox", icon: "✉", perm: "admin.system", tag: "Sample" },
    { href: "contractor-dashboard.html", label: "Contractor portal", icon: "⬒", perm: "admin.system", tag: "Sample" },
  ] };

  // Contractor portal (prototype). Every page in it is sample data.
  function contractorNav() {
    return [
      { label: null, items: [
        { href: "contractor-dashboard.html", label: "Dashboard", icon: "▦" },
        { href: "contractor-bom.html", label: "Bill of materials", icon: "☰" },
        { href: "contractor-rfq.html", label: "Create RFQ", icon: "✚" },
        { href: "contractor-rfqs.html", label: "RFQs & status", icon: "⇄" },
        { href: "contractor-quotes.html", label: "Compare quotes", icon: "⚖" },
      ] },
      { label: "Supplier side", items: [
        { href: "rfq-inbox.html", label: "Supplier RFQ inbox", icon: "✉" },
        { href: CIQ.landingPage(), label: "Back to supplier portal", icon: "←" },
      ] },
    ];
  }

  function navForUser(portal) {
    if (portal === "contractor") return contractorNav();
    const roles = (CIQ.user && CIQ.user.roles) || [];
    const landing = (CIQ.user && CIQ.user.default_landing_page) || "sales-dashboard.html";
    const isAdmin = roles.includes("admin") || CIQ.hasPerm("admin.system");
    const isManager = roles.includes("sales_manager") || CIQ.hasPerm("companies.assign");
    const isEstimator = roles.includes("estimator");
    const isReadOnly = roles.includes("read_only");

    if (isAdmin) {
      return [
        { label: null, items: [
          { href: "admin-dashboard.html", label: "Dashboard", icon: "▦" },
          { href: "opportunities.html", label: "Opportunities", icon: "◎", perm: "projects.view" },
          { href: "my-companies.html", label: "Companies", icon: "⌂", perm: "companies.view" },
          { href: "estimator-work-queue.html", label: "Estimates", icon: "$", perm: "projects.view" },
          { href: "reports.html", label: "Reports", icon: "⊟", perm: "reports.view" },
        ] },
        { label: "Team", items: [
          { href: "assignments.html", label: "Assignments", icon: "⇄", perm: "companies.assign" },
          { href: "team-dashboard.html", label: "Team", icon: "☰", perm: "users.view" },
          { href: "user-management.html", label: "Users", icon: "⦿", perm: "users.create" },
          { href: "catalog-admin.html", label: "Administration", icon: "⚙", perm: "admin.system" },
        ] },
        PREVIEW_GROUP,
      ];
    }
    if (isManager) {
      return [
        { label: null, items: [
          { href: "team-dashboard.html", label: "Dashboard", icon: "▦" },
          { href: "my-companies.html", label: "Companies", icon: "⌂", perm: "crm.relationships.view" },
          { href: "my-tasks.html", label: "Tasks", icon: "✓", perm: "crm.tasks.view" },
          { href: "activity.html", label: "Activity", icon: "≣", perm: "crm.activities.view" },
          { href: "opportunities.html", label: "Opportunities", icon: "◎", perm: "projects.view_assigned" },
          { href: "assignments.html", label: "Assignments", icon: "⇄", perm: "companies.assign" },
          { href: "reports.html", label: "Reports", icon: "⊟", perm: "reports.view" },
        ] },
        { label: "Catalog", items: [
          { href: "product-search.html", label: "Products", icon: "⬡", perm: "products.view" },
        ] },
      ];
    }
    if (isEstimator) {
      return [
        { label: null, items: [
          { href: "estimator-work-queue.html", label: "Work Queue", icon: "▦" },
          { href: "estimator-work-queue.html#mine", label: "My Estimates", icon: "$" },
          { href: "estimator-work-queue.html#submitted", label: "Submitted Estimates", icon: "✓" },
        ] },
      ];
    }
    if (isReadOnly) {
      return [
        { label: null, items: [
          { href: landing, label: "Dashboard", icon: "▦" },
          { href: "my-companies.html", label: "My Companies", icon: "⌂", perm: "crm.relationships.view" },
          { href: "activity.html", label: "Activity", icon: "≣", perm: "crm.activities.view" },
          { href: "reports.html", label: "Reports", icon: "⊟", perm: "reports.view" },
        ] },
      ];
    }
    // Sales representative (default)
    return [
      { label: null, items: [
        { href: "sales-dashboard.html", label: "Dashboard", icon: "▦" },
        { href: "my-companies.html", label: "My Companies", icon: "⌂", perm: "crm.relationships.view" },
        { href: "my-tasks.html", label: "Tasks", icon: "✓", perm: "crm.tasks.view" },
        { href: "activity.html", label: "Activity", icon: "≣", perm: "crm.activities.view" },
        { href: "opportunities.html", label: "Opportunities", icon: "◎", perm: "projects.view_assigned" },
      ] },
      { label: "More", items: [
        { href: "reports.html", label: "Reports", icon: "⊟", perm: "reports.view" },
        { href: "product-search.html", label: "Products", icon: "⬡", perm: "products.view" },
      ] },
    ];
  }

  function navHtml(active, portal) {
    let out = "";
    for (const group of navForUser(portal)) {
      const items = group.items.filter((i) => !i.perm || CIQ.hasPerm(i.perm));
      if (!items.length) continue;
      if (group.label) out += `<div class="nav-group-label">${group.label}</div>`;
      out += items.map((i) => {
        const hrefBase = (i.href || "").split("#")[0];
        const on = active === i.href || active === hrefBase ? " active" : "";
        return `<a class="nav-item${on}" href="${i.href}">
          <span class="ico" aria-hidden="true">${i.icon}</span>${CIQ.esc(i.label)}${i.tag ? `<span class="tag">${CIQ.esc(i.tag)}</span>` : ""}</a>`;
      }).join("");
    }
    return out;
  }

  // Temporary vector mark (final logo artwork pending): a corridor of three
  // ascending bars on the brand-blue tile.
  CIQ.BRAND_MARK = `<svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
    <path d="M3 15.5 L9.2 4.5" stroke="#fff" stroke-width="2.2" stroke-linecap="round"/>
    <path d="M10.4 15.5 L16.6 4.5" stroke="#fff" stroke-width="2.2" stroke-linecap="round" opacity=".55"/>
    <circle cx="16.6" cy="4.5" r="1.9" fill="#5fd3a0"/></svg>`;
  CIQ.BRAND_DESCRIPTOR = "Construction Intelligence + Procurement";

  function portalLabel(portal) {
    if (portal === "contractor") return `<div class="portal-label contractor"><i></i>Contractor portal · Preview</div>`;
    const roles = (CIQ.user && CIQ.user.roles) || [];
    if (roles.includes("admin") || CIQ.hasPerm("admin.system")) return `<div class="portal-label"><i></i>Supplier portal · Admin</div>`;
    return `<div class="portal-label"><i></i>Supplier portal</div>`;
  }

  // Banner for prototype screens. The sample data never comes from, or goes
  // to, the API; this marks every such screen unmistakably.
  CIQ.SAMPLE_BANNER = `<div class="sample-banner" role="note" id="ciqSampleBanner">
    <strong>Sample data — demonstration only</strong>
    <span>Fictional contractor, suppliers, prices and quotes. Nothing on this screen is sent, saved, or purchased.</span></div>`;

  function initials(name) {
    const parts = (name || "?").trim().split(/\s+/);
    return ((parts[0] || "")[0] || "" + ((parts[1] || "")[0] || "")).toUpperCase() +
      (parts[1] ? parts[1][0].toUpperCase() : "");
  }

  function primaryRole(portal) {
    if (portal === "contractor") return "Previewing as contractor";
    const r = (CIQ.user && CIQ.user.roles) || [];
    const map = { admin: "Administrator", sales_manager: "Sales Manager",
      sales_representative: "Sales Rep", estimator: "Estimator",
      read_only: "Read Only", fulfillment_user: "Fulfillment" };
    return map[r[0]] || (r[0] || "Employee");
  }

  CIQ.renderShell = function (opts) {
    opts = opts || {};
    const app = document.createElement("div");
    app.className = "app"; app.id = "ciqApp";
    app.innerHTML = `
      <div class="scrim" id="ciqScrim"></div>
      <aside class="sidebar" id="ciqSidebar">
        <a class="sidebar-brand" href="${CIQ.esc(opts.portal === "contractor" ? "contractor-dashboard.html" : CIQ.landingPage())}">
          <div class="brand-mark">${CIQ.BRAND_MARK}</div>
          <div class="brand-text"><div class="t">CorridorIQ</div><div class="s">${CIQ.esc(CIQ.BRAND_DESCRIPTOR)}</div></div>
        </a>
        ${portalLabel(opts.portal)}
        <nav class="nav">${navHtml(opts.active, opts.portal)}</nav>
        <div class="sidebar-foot">
          <a class="nav-item" href="#" id="ciqSignout"><span class="ico">⇥</span>Sign out</a>
          <div class="domain">CorridorIQ.pro</div>
        </div>
      </aside>
      <div class="main">
        <header class="topbar">
          <button class="icon-btn mobile-only" id="ciqBurger" aria-label="Menu">☰</button>
          <div class="stack">
            <h1>${CIQ.esc(opts.title || "")}</h1>
            ${opts.subtitle ? `<div class="subtitle">${CIQ.esc(opts.subtitle)}</div>` : ""}
          </div>
          <form class="topbar-search" id="ciqSearchForm"${opts.portal === "contractor" ? ' style="visibility:hidden"' : ""}>
            <span class="si">⌕</span>
            <input type="search" id="ciqSearch" placeholder="Search companies…" autocomplete="off" />
          </form>
          <div class="topbar-actions">
            ${opts.sample ? '<span class="sample-pill" title="Fictional demonstration data">Sample data</span>' : ""}
            <button class="icon-btn" id="ciqTasksBtn" title="Tasks" aria-label="Tasks"${opts.portal === "contractor" ? ' style="display:none"' : ""}>✓</button>
            <div class="profile" id="ciqProfile" tabindex="0">
              <div class="avatar">${CIQ.esc(initials(CIQ.user.display_name || CIQ.user.email))}</div>
              <div class="stack">
                <span class="profile-name">${CIQ.esc(CIQ.user.display_name || CIQ.user.email)}</span>
                <span class="profile-role">${CIQ.esc(primaryRole(opts.portal))}</span>
              </div>
              <div class="menu" id="ciqMenu">
                <a href="login.html?change=1">Change password</a>
                <button type="button" id="ciqMenuSignout">Sign out</button>
              </div>
            </div>
          </div>
        </header>
        <div class="content" id="content"></div>
      </div>`;
    // Move page content (from <template id="pageContent">) into #content.
    const tpl = document.getElementById("pageContent");
    document.body.innerHTML = "";
    document.body.appendChild(app);
    let host = document.getElementById("toastHost");
    if (!host) { host = document.createElement("div"); host.id = "toastHost"; document.body.appendChild(host); }
    if (opts.sample) app.querySelector("#content").insertAdjacentHTML("beforeend", CIQ.SAMPLE_BANNER);
    if (tpl) app.querySelector("#content").appendChild(tpl.content.cloneNode(true));

    // Wire shell interactions.
    const go = (e) => { e.preventDefault(); CIQ.logout(); };
    app.querySelector("#ciqSignout").addEventListener("click", go);
    app.querySelector("#ciqMenuSignout").addEventListener("click", go);
    app.querySelector("#ciqTasksBtn").addEventListener("click", () => location.href = "my-tasks.html");
    const burger = app.querySelector("#ciqBurger");
    burger.addEventListener("click", () => app.classList.toggle("nav-open"));
    app.querySelector("#ciqScrim").addEventListener("click", () => app.classList.remove("nav-open"));
    const prof = app.querySelector("#ciqProfile"), menu = app.querySelector("#ciqMenu");
    prof.addEventListener("click", (e) => { if (e.target.closest(".menu")) return; menu.classList.toggle("open"); });
    document.addEventListener("click", (e) => { if (!prof.contains(e.target)) menu.classList.remove("open"); });
    const sform = app.querySelector("#ciqSearchForm");
    sform.addEventListener("submit", (e) => {
      e.preventDefault();
      const q = app.querySelector("#ciqSearch").value.trim();
      location.href = "my-companies.html" + (q ? "?q=" + encodeURIComponent(q) : "");
    });
    return app.querySelector("#content");
  };

  CIQ.setTaskCount = function (n) {
    const b = document.getElementById("ciqTasksBtn");
    if (!b) return;
    let c = b.querySelector(".count");
    if (n > 0) {
      if (!c) { c = document.createElement("span"); c.className = "count"; b.appendChild(c); }
      c.textContent = n;
    } else if (c) { c.remove(); }
  };

  /* ---- Guard -------------------------------------------------------- */
  CIQ.landingPage = function () {
    return (CIQ.user && CIQ.user.default_landing_page) || "sales-dashboard.html";
  };

  CIQ.guard = async function (requiredPerm, opts) {
    opts = opts || {};
    let me;
    try { me = await CIQ.api.get("/api/auth/me"); }
    catch (e) { location.href = "login.html"; return null; }
    CIQ.user = me.user; authed = true;
    if (CIQ.user.must_change_password && !location.pathname.endsWith("login.html")) {
      location.href = "login.html?change=1"; return null;
    }
    const anyPerm = opts.anyPerm;
    let allowed = true;
    if (anyPerm && anyPerm.length) {
      allowed = anyPerm.some((p) => CIQ.hasPerm(p));
    } else if (requiredPerm) {
      allowed = CIQ.hasPerm(requiredPerm);
    }
    // Role fallback: administrators always reach admin-only pages.
    if (!allowed && requiredPerm === "admin.system") {
      const roles = (CIQ.user && CIQ.user.roles) || [];
      allowed = roles.includes("admin");
    }
    if (!allowed) {
      location.href = CIQ.landingPage();
      return null;
    }
    CIQ.content = CIQ.renderShell(opts);
    return CIQ.user;
  };

  /* ---- Toast -------------------------------------------------------- */
  CIQ.toast = function (msg, type = "info", ms = 3200) {
    const host = document.getElementById("toastHost");
    if (!host) return;
    const t = document.createElement("div");
    t.className = "toast " + type;
    t.innerHTML = `<span>${CIQ.esc(msg)}</span>`;
    host.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; setTimeout(() => t.remove(), 200); }, ms);
  };

  /* ---- Modal / confirm --------------------------------------------- */
  CIQ.modal = function ({ title, body, footer, onMount, width }) {
    const back = document.createElement("div");
    back.className = "modal-backdrop";
    back.innerHTML = `<div class="modal" role="dialog" aria-modal="true" ${width ? `style="max-width:${width}px"` : ""}>
      <div class="modal-head"><h3>${CIQ.esc(title || "")}</h3>
        <button class="modal-close" aria-label="Close">&times;</button></div>
      <div class="modal-body"></div>
      ${footer !== undefined ? `<div class="modal-foot"></div>` : ""}</div>`;
    back.querySelector(".modal-body").innerHTML = typeof body === "string" ? body : "";
    if (typeof body !== "string" && body) back.querySelector(".modal-body").appendChild(body);
    if (footer && footer instanceof Node) back.querySelector(".modal-foot").appendChild(footer);
    else if (typeof footer === "string") back.querySelector(".modal-foot").innerHTML = footer;
    const close = () => back.remove();
    back.querySelector(".modal-close").addEventListener("click", close);
    back.addEventListener("mousedown", (e) => { if (e.target === back) close(); });
    document.addEventListener("keydown", function esc(e) {
      if (e.key === "Escape") { close(); document.removeEventListener("keydown", esc); }
    });
    document.body.appendChild(back);
    if (onMount) onMount(back, close);
    return { el: back, close };
  };

  CIQ.confirm = function (message, { danger = false, confirmLabel = "Confirm" } = {}) {
    return new Promise((resolve) => {
      const foot = document.createElement("div");
      foot.innerHTML = `<button class="btn" data-a="cancel">Cancel</button>
        <button class="btn ${danger ? "btn-danger" : "btn-primary"}" data-a="ok">${CIQ.esc(confirmLabel)}</button>`;
      const m = CIQ.modal({ title: "Please confirm", body: `<p>${CIQ.esc(message)}</p>`, footer: foot });
      foot.querySelector('[data-a="cancel"]').addEventListener("click", () => { m.close(); resolve(false); });
      foot.querySelector('[data-a="ok"]').addEventListener("click", () => { m.close(); resolve(true); });
    });
  };

  /* ---- Log Activity (reusable, Phase 6) ---------------------------- */
  const OUTCOMES = [
    ["", "—"], ["no_answer", "No Answer"], ["left_voicemail", "Left Voicemail"],
    ["spoke_with_contact", "Spoke With Contact"], ["interested", "Interested"],
    ["follow_up_requested", "Follow-Up Requested"], ["appointment_set", "Appointment Set"],
    ["quote_requested", "Quote Requested"], ["not_interested", "Not Interested"],
    ["wrong_number", "Wrong Number"], ["do_not_contact", "Do Not Contact"],
  ];
  const TYPES = [
    ["call", "Call"], ["voicemail", "Voicemail"], ["email", "Email"],
    ["text_message", "Text"], ["meeting", "Meeting"], ["site_visit", "Site Visit"],
    ["note", "Note"], ["quote_request", "Quote Request"], ["follow_up", "Follow-Up"],
  ];
  const STATUS_OPTS = [
    ["", "No change"], ["contacted", "Contacted"], ["qualified", "Qualified"],
    ["follow_up", "Follow-Up"], ["quote_requested", "Quote Requested"],
    ["quote_sent", "Quote Sent"], ["negotiating", "Negotiating"], ["won", "Won"],
    ["lost", "Lost"], ["do_not_contact", "Do Not Contact"],
  ];

  CIQ.logActivity = function ({ companyId, companyName, prefill = {}, projects = [], onSaved }) {
    const nowLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000)
      .toISOString().slice(0, 16);
    const projOpts = ['<option value="">— none —</option>']
      .concat(projects.map((p) => `<option value="${p.project_id || p.id}">${CIQ.esc(
        (p.job_address || p.address || p.permit_number || ("Project #" + (p.project_id || p.id))))}</option>`)).join("");
    const body = document.createElement("form");
    body.className = "form-grid";
    body.innerHTML = `
      <label class="fld">Activity type
        <select name="activity_type">${TYPES.map(([v, l]) =>
          `<option value="${v}" ${prefill.activity_type === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label class="fld">Outcome
        <select name="activity_outcome">${OUTCOMES.map(([v, l]) =>
          `<option value="${v}" ${prefill.activity_outcome === v ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label class="fld">Contact person
        <input name="subject" placeholder="Who did you speak with?" value="${CIQ.esc(prefill.subject || "")}" /></label>
      <label class="fld">When
        <input type="datetime-local" name="activity_at" value="${nowLocal}" /></label>
      <label class="fld full">Notes
        <textarea name="notes" placeholder="What happened?">${CIQ.esc(prefill.notes || "")}</textarea></label>
      <label class="fld">Related project
        <select name="project_id">${projOpts}</select></label>
      <label class="fld">Next follow-up
        <input type="datetime-local" name="next_followup_at" /></label>
      <label class="fld full">Update relationship status (optional)
        <select name="relationship_status">${STATUS_OPTS.map(([v, l]) =>
          `<option value="${v}">${l}</option>`).join("")}</select></label>`;

    const foot = document.createElement("div");
    foot.innerHTML = `<button type="button" class="btn" data-a="cancel">Cancel</button>
      <button type="button" class="btn btn-primary" data-a="save">Save activity</button>`;

    let dirty = false;
    body.addEventListener("input", () => { dirty = true; });

    const m = CIQ.modal({
      title: companyName ? `Log activity · ${companyName}` : "Log activity",
      body, footer: foot,
    });

    foot.querySelector('[data-a="cancel"]').addEventListener("click", async () => {
      if (dirty && body.querySelector('[name="notes"]').value.trim()) {
        if (!(await CIQ.confirm("Discard your unsaved notes?", { danger: true, confirmLabel: "Discard" }))) return;
      }
      m.close();
    });

    const saveBtn = foot.querySelector('[data-a="save"]');
    saveBtn.addEventListener("click", async () => {
      const fd = new FormData(body);
      const payload = {
        activity_type: fd.get("activity_type"),
        activity_outcome: fd.get("activity_outcome") || null,
        subject: (fd.get("subject") || "").trim() || null,
        notes: (fd.get("notes") || "").trim() || null,
        activity_at: fd.get("activity_at") ? fd.get("activity_at") + ":00" : null,
        project_id: fd.get("project_id") ? Number(fd.get("project_id")) : null,
        next_followup_at: fd.get("next_followup_at") ? fd.get("next_followup_at") + ":00" : null,
      };
      const newStatus = fd.get("relationship_status");
      // Duplicate-submit prevention + disabled state.
      saveBtn.disabled = true; saveBtn.textContent = "Saving…";
      try {
        if ((newStatus === "lost" || newStatus === "do_not_contact")) {
          const ok = await CIQ.confirm(
            newStatus === "lost" ? "Mark this company as Lost?" : "Set this company to Do Not Contact?",
            { danger: true, confirmLabel: "Yes, continue" });
          if (!ok) { saveBtn.disabled = false; saveBtn.textContent = "Save activity"; return; }
        }
        const act = await CIQ.api.post(`/api/sales/companies/${companyId}/activities`, payload);
        if (newStatus) {
          await CIQ.api.patch(`/api/sales/companies/${companyId}/relationship`, { relationship_status: newStatus });
        }
        CIQ.toast("Activity saved", "success");
        m.close();
        if (onSaved) onSaved(act, newStatus || null);
      } catch (e) {
        CIQ.toast(e.message || "Could not save activity", "error");
        saveBtn.disabled = false; saveBtn.textContent = "Save activity";
      }
    });
    return m;
  };

  /* ---- Badges & formatting ----------------------------------------- */
  const STATUS_LABEL = {
    new: "New", assigned: "Assigned", researching: "Researching",
    attempted_contact: "Attempted", contacted: "Contacted", qualified: "Qualified",
    follow_up: "Follow-Up", quote_requested: "Quote Requested", quote_sent: "Quote Sent",
    negotiating: "Negotiating", won: "Won", lost: "Lost",
    do_not_contact: "Do Not Contact", inactive: "Inactive",
  };
  const STATUS_COLOR = {
    won: "green", lost: "red", do_not_contact: "red", quote_requested: "gold",
    quote_sent: "gold", qualified: "blue", negotiating: "blue", contacted: "amber",
    follow_up: "amber", researching: "slate", attempted_contact: "amber",
    new: "slate", assigned: "slate", inactive: "slate",
  };
  const TIER_COLOR = { Critical: "red", High: "amber", Medium: "blue", Low: "slate" };

  CIQ.statusLabel = (s) => STATUS_LABEL[s] || CIQ.titleCase(s || "");
  CIQ.statusBadge = (s) =>
    `<span class="badge ${STATUS_COLOR[s] || "slate"}"><span class="dot"></span>${CIQ.esc(CIQ.statusLabel(s))}</span>`;
  CIQ.tierBadge = (t) => t ? `<span class="badge ${TIER_COLOR[t] || "slate"}">${CIQ.esc(t)}</span>` : "";
  // Supplier-facing screens show priority labels, never the underlying number.
  CIQ.scoreChip = (v) => CIQ.bandBadge(v);
  CIQ.priorityBadge = (tier) => CIQ.tierBadge(tier);

  CIQ.activityLabel = (t) => ({
    call: "Call", voicemail: "Voicemail", email: "Email", text_message: "Text",
    meeting: "Meeting", site_visit: "Site Visit", note: "Note",
    quote_request: "Quote Request", quote_sent: "Quote Sent", follow_up: "Follow-Up",
    status_change: "Status Change", assignment: "Assignment",
  }[t] || CIQ.titleCase(t || ""));
  CIQ.outcomeLabel = (o) => (OUTCOMES.find((x) => x[0] === o) || [o, CIQ.titleCase(o || "")])[1];

  CIQ.titleCase = (s) => String(s || "").replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());

  /* ---- Dates -------------------------------------------------------- */
  CIQ.fmtDate = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso); if (isNaN(d)) return String(iso).slice(0, 10);
    return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
  };
  CIQ.fmtDateTime = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso); if (isNaN(d)) return String(iso);
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  };
  CIQ.relTime = (iso) => {
    if (!iso) return "—";
    const d = new Date(iso); if (isNaN(d)) return String(iso).slice(0, 10);
    const s = (Date.now() - d.getTime()) / 1000;
    if (s < 0) { // future
      const days = Math.round(-s / 86400);
      if (days === 0) return "today";
      if (days === 1) return "tomorrow";
      return "in " + days + "d";
    }
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    const days = Math.floor(s / 86400);
    if (days < 30) return days + "d ago";
    return CIQ.fmtDate(iso);
  };
  CIQ.money = (v) => v == null ? "—" :
    "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
  CIQ.moneyCompact = function (v) {
    if (v == null || v === "") return "—";
    const n = Number(v);
    if (!isFinite(n)) return "—";
    const abs = Math.abs(n);
    if (abs >= 1e6) {
      const num = n / 1e6;
      return "$" + (abs >= 10e6 ? Math.round(num) : Math.round(num * 10) / 10) + "M";
    }
    if (abs >= 1e3) {
      const num = n / 1e3;
      return "$" + (abs >= 10e3 ? Math.round(num) : Math.round(num * 10) / 10) + "K";
    }
    return CIQ.money(n);
  };

  /* ---- Utilities ---------------------------------------------------- */
  CIQ.esc = function (s) {
    if (s === null || s === undefined) return "";
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  };
  CIQ.qs = (name) => new URLSearchParams(location.search).get(name);
  CIQ.debounce = function (fn, ms = 300) {
    let t; return function (...a) { clearTimeout(t); t = setTimeout(() => fn.apply(this, a), ms); };
  };
  CIQ.busy = async function (btn, fn) {
    if (btn.disabled) return;
    const label = btn.textContent; btn.disabled = true; btn.dataset.busy = "1";
    try { return await fn(); }
    finally { btn.disabled = false; btn.textContent = label; delete btn.dataset.busy; }
  };
  CIQ.skeletonRows = (n = 5) =>
    Array.from({ length: n }).map(() => `<div class="skel skel-row"></div>`).join("");
  CIQ.emptyState = ({ icon = "◎", title = "Nothing here yet", text = "", action = "", compact = false }) =>
    `<div class="empty-state${compact ? " sm" : ""}"><div class="ico">${icon}</div><h3>${CIQ.esc(title)}</h3>
     <p>${CIQ.esc(text)}</p>${action}</div>`;
  CIQ.errorBanner = (msg) => `<div class="error-banner">${CIQ.esc(msg)}</div>`;

  // Account relevance from the server-side trust layer (sales-lane assignment).
  const RELEVANCE_COLOR = { CORE: "green", ADJACENT: "blue", NOT_ASSESSED: "slate",
    NOT_RELEVANT: "amber", NOT_SALES_READY: "red" };
  CIQ.relevanceBadge = (rel) => rel && rel.label
    ? `<span class="badge ${RELEVANCE_COLOR[rel.status] || "slate"}" title="${CIQ.esc((rel.lanes || []).join(", "))}">${CIQ.esc(rel.label)}</span>`
    : "";

  // Best verified contact for one company (server: pipeline/trust/contacts.py).
  // Only this company's own verified values are shown as usable; linked-record
  // values are labelled inherited. Nothing is filled in on the client.
  CIQ.verifiedContactBlock = function (vc, opts) {
    const o = opts || {};
    if (!vc || vc.status !== "VERIFIED") {
      const inherited = (vc && vc.inherited) || [];
      return `<div class="contact-block">
        <div><span class="badge amber">${CIQ.esc((vc && vc.status === "INHERITED_UNVERIFIED") ? "Inherited — not verified" : "Not verified")}</span>
          <b>Contact not yet verified.</b></div>
        ${inherited.length ? `<div class="muted" style="font-size:12px;margin-top:4px">${inherited.map((i) =>
          `${CIQ.esc(i.type)}: ${CIQ.esc(i.value)}${i.name ? " (" + CIQ.esc(i.name) + ")" : ""} — ${CIQ.esc(i.note)}`).join("<br>")}</div>` : ""}
      </div>`;
    }
    const when = (x) => x && x.verified_at ? ` · verified ${CIQ.esc(CIQ.fmtDate(x.verified_at))}` : "";
    const src = (x) => x ? ` <span class="muted" style="font-size:12px">(${CIQ.esc(x.source)}${when(x)})</span>` : "";
    const person = [vc.contact_name, vc.title].filter(Boolean).join(", ");
    const rows = [];
    if (!o.compact || vc.contact_name) rows.push(`<div><span>Contact</span>${CIQ.esc(person || "Business line (no named contact)")}</div>`);
    if (vc.phone && (!o.compact || o.show !== "email"))
      rows.push(`<div><span>Phone</span><a href="tel:${CIQ.esc(vc.phone.value)}">${CIQ.esc(vc.phone.value)}</a>${src(vc.phone)}</div>`);
    if (vc.email && (!o.compact || o.show !== "phone"))
      rows.push(`<div><span>Email</span><a href="mailto:${CIQ.esc(vc.email.value)}">${CIQ.esc(vc.email.value)}</a>${src(vc.email)}</div>`);
    if (vc.address && !o.compact) rows.push(`<div><span>Address</span>${CIQ.esc(vc.address.value)}${src(vc.address)}</div>`);
    if (vc.website && !o.compact) rows.push(`<div><span>Website</span>${CIQ.esc(vc.website.value)}</div>`);
    return `<div class="contact-block">
      <div><span class="badge green">Verified</span> ${CIQ.esc(vc.status_label)}${vc.last_verified ? ` · last verified ${CIQ.esc(CIQ.fmtDate(vc.last_verified))}` : ""}</div>
      <div class="cc-meta">${rows.join("")}</div>
    </div>`;
  };

  // Trust-checked accounts for today. The server decides who is listed; the
  // list is never padded, so an empty list is a real answer.
  CIQ.renderTodaysAccounts = function (el, payload) {
    if (!el) return;
    const p = payload || {};
    const items = p.items || [];
    const ds = p.data_status || {};
    const status = ds.note ? `<div class="muted" style="margin-bottom:8px">${CIQ.esc(ds.note)}</div>` : "";
    if (!items.length) {
      el.innerHTML = status + CIQ.emptyState({ compact: true, icon: "◎", title: "No accounts passed today's checks",
        text: p.note || "The list is empty on purpose; it is not padded." });
      return;
    }
    el.className = "grid-cards";
    el.innerHTML = status + items.map((a) => {
      const pr = a.project || {};
      const list = (xs) => (xs || []).map((x) => `<li>${CIQ.esc(CIQ.cleanText(x))}</li>`).join("");
      const checks = (a.verify_before_contact || []).length + (a.uncertainty || []).length;
      return `<div class="company-card acct-card">
        <div class="cc-top">
          <div>
            <div class="cc-name"><a href="sales-company-profile.html?id=${a.company_id}">${CIQ.esc(a.display_name)}</a></div>
            <div class="cc-loc">${CIQ.esc([pr.address, pr.city].filter(Boolean).join(", ") || "—")}</div>
          </div>
          <div class="stack" style="align-items:flex-end;gap:6px">
            <span class="badge green">${CIQ.esc(a.action)}</span>
            <span class="badge outline">${CIQ.esc(a.confidence)} confidence</span>
          </div>
        </div>
        <div class="acct-why"><b>Why now</b>${CIQ.esc(CIQ.cleanText(a.why_now))}</div>
        <div class="cc-meta">
          <div><span>Trade</span>${CIQ.esc(CIQ.titleCase(a.trade || "—"))}</div>
          <div><span>Permit</span>${CIQ.esc(pr.permit_number || "—")} · ${CIQ.esc(pr.activity_date ? CIQ.fmtDate(pr.activity_date) : "—")}</div>
          <div><span>Business identity</span>${CIQ.esc(a.identity)}</div>
          <div><span>Contact</span>${CIQ.esc(a.contact || "—")}</div>
        </div>
        ${CIQ.verifiedContactBlock(a.contact_details, { compact: true, show: a.action === "Email" ? "email" : "phone" })}
        ${checks ? `<details><summary>Before you call · ${checks} check${checks === 1 ? "" : "s"}</summary>
          ${a.uncertainty && a.uncertainty.length ? `<div style="margin-top:6px"><strong>Uncertain</strong><ul class="cc-list">${list(a.uncertainty)}</ul></div>` : ""}
          ${(a.verify_before_contact || []).length ? `<div style="margin-top:6px"><strong>Verify before contact</strong><ul class="cc-list">${list(a.verify_before_contact)}</ul></div>` : ""}
        </details>` : ""}
      </div>`;
    }).join("");
  };

  CIQ.todayUTC = () => new Date().toISOString().slice(0, 10);
  CIQ.daysSince = function (iso) {
    if (!iso) return null;
    const t = Date.parse(String(iso).slice(0, 10) + "T00:00:00Z");
    if (isNaN(t)) return null;
    return Math.floor((Date.now() - t) / 86400000);
  };

  CIQ.whyNow = function (p) {
    const timing = CIQ.titleCase(p.opportunity_timing || "");
    const stage = CIQ.titleCase((p.project_lifecycle || "").replace(/_/g, " "));
    const when = p.opportunity_date ? CIQ.fmtDate(p.opportunity_date) : "";
    const age = CIQ.daysSince(p.opportunity_date);
    if (age === 0) return "New today — first contact window is open.";
    if (age != null && age <= 2)
      return `Updated ${age === 1 ? "yesterday" : age + "d ago"}${stage ? " · " + stage : ""}.`;
    if (/excellent|immediate/i.test(timing))
      return `Act now — buying window is open${stage ? ", " + stage.toLowerCase() : ""}.`;
    if (/good/i.test(timing))
      return `Good window to call${stage ? " during " + stage.toLowerCase() : ""}.`;
    if (/late/i.test(timing))
      return `Window closing${stage ? " — " + stage.toLowerCase() : ""}.`;
    if (/historical/i.test(timing))
      return "Historical job — context only, not a live buy.";
    if (stage && when) return `${stage} as of ${when}.`;
    if (stage) return `${stage} on your book.`;
    return "Ranked opportunity on an assigned company.";
  };

  CIQ.bestContact = function (meta) {
    meta = meta || {};
    const name = (meta.full_name || meta.contact_name || "").trim();
    const title = (meta.job_title || "").trim();
    const role = (meta.primary_role || "").trim();
    const phone = (meta.phone || meta.main_phone || "").trim();
    const named = !!(name || phone);
    let label = "No contact found";
    if (name && title) label = name + " · " + title;
    else if (name && phone) label = name + " · " + phone;
    else if (name) label = name;
    else if (phone) label = phone;
    else if (title) label = title;
    else if (role) label = CIQ.titleCase(role.replace(/_/g, " ")) + " (role on file)";
    return { label, named, phone };
  };
  CIQ.bestContactLabel = function (meta) { return CIQ.bestContact(meta).label; };

  // Contact for an opportunity row. The API attaches the same verified view the
  // company page shows; only verified values count as a usable contact.
  CIQ.itemContact = function (p, meta) {
    const c = p && p.contact;
    if (!c) return CIQ.bestContact(meta);
    if (c.status !== "VERIFIED") return { label: c.label || "Contact not yet verified.", named: false, phone: "" };
    const who = [c.name, c.title].filter(Boolean).join(" · ");
    const how = c.phone || c.email || "";
    return { label: [who || "Business line", how].filter(Boolean).join(" · "), named: true, phone: c.phone || "" };
  };

  // Customer-facing priority band. Presentation only: the underlying number
  // and ranking are unchanged and never rendered on supplier screens.
  CIQ.oppBand = function (score) {
    if (score == null || score === "") return { key: "none", label: "Not rated", score: null };
    const n = Number(score);
    if (!isFinite(n)) return { key: "none", label: "Not rated", score: null };
    if (n >= 85) return { key: "high", label: "High priority", score: n };
    if (n >= 70) return { key: "mid", label: "Priority", score: n };
    return { key: "low", label: "Monitor", score: n };
  };
  CIQ.bandBadge = function (score) {
    const b = CIQ.oppBand(score);
    return `<span class="band ${b.key}">${CIQ.esc(b.label)}</span>`;
  };

  // Readable place names for jurisdiction codes in server text ("phoenix_az").
  CIQ.placeName = (j) => CIQ.titleCase(String(j || "").replace(/_az$/i, "").replace(/_/g, " "));
  CIQ.cleanText = (s) => String(s || "")
    .replace(/\(([a-z]+(?:_[a-z]+)*)_az\)/g, (m, city) => "(" + CIQ.placeName(city) + ")")
    .replace(/\b([a-z]+(?:_[a-z]+)*)_az\b/g, (m, city) => CIQ.placeName(city));

  CIQ.oppSignals = function (p, contact) {
    const out = [];
    const age = CIQ.daysSince(p.opportunity_date);
    const stage = String(p.project_lifecycle || "").toLowerCase();
    const value = Number(p.estimated_material_value);
    if (contact && contact.named) out.push({ t: "Contact available", k: "pos" });
    else out.push({ t: "No contact found", k: "warn" });
    if (age === 0 || (age != null && age <= 3 && /submitted|application|permitting/.test(stage)))
      out.push({ t: "New permit", k: "" });
    else if (age != null && age <= 2) out.push({ t: "Recently updated", k: "" });
    else if (age != null && age <= 7) out.push({ t: "Recent permit activity", k: "" });
    if (isFinite(value) && value >= 100000) out.push({ t: "Large project", k: "" });
    if (p.permit_count != null && Number(p.permit_count) >= 3)
      out.push({ t: "Repeat contractor", k: "" });
    return out.slice(0, 3);
  };

  CIQ.oppDetailsHref = function (p) {
    if (p && p.company_id) return "sales-company-profile.html?id=" + p.company_id;
    return "opportunities.html";
  };

  CIQ.summarizeOpportunities = function (opts) {
    opts = opts || {};
    const items = opts.items || [];
    const k = opts.kpis || {};
    const meta = opts.companyMeta || {};
    const today = CIQ.todayUTC();
    const pipeline = items.reduce((s, p) => s + (Number(p.estimated_material_value) || 0), 0);
    const newToday = items.filter((p) => String(p.opportunity_date || "").slice(0, 10) === today).length;
    const missing = items.filter((p) => !CIQ.itemContact(p, meta[p.company_id]).named).length;
    return {
      highPriority: k.high_priority_opportunities != null
        ? k.high_priority_opportunities
        : items.filter((p) => (p.opportunity_score || 0) >= 85).length,
      pipelineValue: pipeline,
      newToday: k.new_submitted_opportunities != null ? k.new_submitted_opportunities : newToday,
      missingContact: missing,
      total: opts.total != null ? opts.total : items.length,
    };
  };

  CIQ.renderExecStrip = function (el, stats, opts) {
    if (!el) return;
    opts = opts || {};
    const listHref = opts.listHref || "opportunities.html";
    const s = stats || {};
    const blocks = [
      { k: "High priority", v: Number(s.highPriority || 0).toLocaleString("en-US"), href: listHref, cls: "" },
      { k: "Pipeline value", v: CIQ.moneyCompact(s.pipelineValue || 0), href: listHref, cls: "", title: CIQ.money(s.pipelineValue || 0) },
      { k: "New today", v: Number(s.newToday || 0).toLocaleString("en-US"), href: listHref, cls: "" },
      { k: "Missing contact", v: Number(s.missingContact || 0).toLocaleString("en-US"), href: "my-companies.html", cls: s.missingContact ? "is-warn" : "" },
    ];
    el.className = "dash-strip";
    el.innerHTML = blocks.map((b) =>
      `<a class="dash-strip-item ${b.cls}" href="${CIQ.esc(b.href)}"${b.title ? ` title="${CIQ.esc(b.title)}"` : ""}>
        <div class="dash-strip-k">${CIQ.esc(b.k)}</div>
        <div class="dash-strip-v">${CIQ.esc(b.v)}</div>
      </a>`).join("");
  };

  CIQ.renderTopOpportunities = function (el, opts) {
    opts = opts || {};
    const all = opts.items || [];
    const shown = all.slice(0, opts.limit || 6);
    const listHref = opts.listHref || "opportunities.html";
    const byCompany = opts.companyMeta || {};
    const total = opts.total != null ? opts.total : all.length;
    el.className = "";
    const head = `<div class="dash-head">
      <div><h2>Top opportunities</h2>
        <span class="sub">${CIQ.esc(opts.contextLabel || "Best revenue openings right now")}${total ? " · " + total + " in this view" : ""}</span></div>
      <a href="${CIQ.esc(listHref)}">View all</a>
    </div>`;
    if (!shown.length) {
      el.innerHTML = head + CIQ.emptyState({
        compact: true, icon: "◎", title: "No ranked opportunities",
        text: opts.emptyMessage || "Projects tied to assigned companies will appear here, scored and ordered.",
        action: `<a class="btn btn-sm" href="${CIQ.esc(listHref)}">Open opportunities</a>`,
      });
      return;
    }
    const rows = shown.map((p, i) => {
      const project = p.job_address || p.permit_number || p.project_category || "Project";
      const loc = [p.city || p.jurisdiction, p.state].filter(Boolean).join(", ")
        || p.jurisdiction || "";
      const locLine = [p.permit_number, loc].filter(Boolean).join(" · ");
      const hasVal = p.estimated_material_value != null && p.estimated_material_value !== "";
      const contact = CIQ.itemContact(p, byCompany[p.company_id]);
      const band = CIQ.oppBand(p.opportunity_score);
      const href = CIQ.oppDetailsHref(p);
      const sigs = CIQ.oppSignals(p, contact).map((s) =>
        `<span class="sig ${s.k}">${CIQ.esc(s.t)}</span>`).join("");
      return `<article class="opp-row${i === 0 ? " is-top" : ""}">
        <div class="opp-rank">${String(i + 1).padStart(2, "0")}</div>
        <div>
          <a class="opp-co" href="${href}">${CIQ.esc(p.display_name || "—")}</a>
          <div class="opp-job">${CIQ.esc(project)}${locLine ? ` <span class="opp-loc">· ${CIQ.esc(locLine)}</span>` : ""}</div>
          <div class="opp-sigs">${sigs}</div>
        </div>
        <div>
          <div class="opp-val${hasVal ? "" : " is-empty"}" title="${hasVal ? CIQ.esc(CIQ.money(p.estimated_material_value)) : ""}">${hasVal ? CIQ.esc(CIQ.moneyCompact(p.estimated_material_value)) : "—"}</div>
          <div class="opp-val-k">${band.score != null ? CIQ.esc(band.label) : "Est. value"}</div>
        </div>
        <div class="opp-contact-col">
          <div class="opp-contact${contact.named ? "" : " is-missing"}">${CIQ.esc(contact.label)}</div>
          <div class="opp-why">${CIQ.esc(CIQ.whyNow(p))}</div>
        </div>
        <div class="opp-act"><a class="btn btn-sm" href="${href}">Open</a></div>
      </article>`;
    }).join("");
    el.innerHTML = `${head}<div class="opp-feed">${rows}</div>`;
  };

  /* ---- Dashboard hero + procurement entry --------------------------- */
  CIQ.greeting = function () {
    const h = new Date().getHours();
    return h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
  };
  CIQ.renderHero = function (opts) {
    const o = opts || {};
    const date = document.getElementById("heroDate");
    const title = document.getElementById("heroTitle");
    const today = new Date().toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
    if (date) date.textContent = `${today} · ${o.region || "Phoenix metro"}`;
    const dn = (CIQ.user && CIQ.user.display_name) || "";
    const first = (CIQ.user && CIQ.user.first_name) || (dn && !dn.includes("@") ? dn.split(" ")[0] : "");
    if (title) title.textContent = `${CIQ.greeting()}${first ? ", " + first : ""}.`;
  };

  // RFQ inbox entry. Administrators see the sample preview; everyone else sees
  // nothing until supplier RFQ participation exists.
  CIQ.renderRfqEntry = function (el) {
    if (!el) return;
    if (!CIQ.hasPerm("admin.system")) { el.style.display = "none"; return; }
    el.innerHTML = `<div class="card">
      <div class="card-head"><div><h3>RFQ inbox</h3><div class="sub">Contractor requests for quote</div></div>
        <span class="sample-pill">Sample</span></div>
      <div class="card-body" style="font-size:13px;color:var(--text-2)">
        Contractor RFQs are in early access. Preview the supplier side of the planned workflow with sample data —
        no real RFQs, quotes, or supplier responses exist yet.
      </div>
      <div class="card-foot row">
        <a class="btn btn-sm btn-primary" href="rfq-inbox.html">Open RFQ inbox</a>
        <a class="btn btn-sm btn-ghost" href="contractor-dashboard.html">Contractor view</a>
      </div>
    </div>`;
  };

  CIQ.pager = function (el, { page, pages, total, onPage }) {
    if (!pages || pages <= 1) { el.innerHTML = total != null ? `<span class="muted">${total} total</span>` : ""; return; }
    el.innerHTML = `<button class="btn btn-sm" ${page <= 1 ? "disabled" : ""} data-a="prev">← Prev</button>
      <span>Page ${page} of ${pages}${total != null ? ` · ${total} total` : ""}</span>
      <button class="btn btn-sm" ${page >= pages ? "disabled" : ""} data-a="next">Next →</button>`;
    const p = el.querySelector('[data-a="prev"]'), n = el.querySelector('[data-a="next"]');
    if (p) p.addEventListener("click", () => onPage(page - 1));
    if (n) n.addEventListener("click", () => onPage(page + 1));
  };

  /* Warn before leaving with unsaved input. */
  CIQ.guardUnsaved = function (isDirty) {
    window.addEventListener("beforeunload", (e) => {
      if (isDirty()) { e.preventDefault(); e.returnValue = ""; }
    });
  };

  window.CIQ = CIQ;
})();
