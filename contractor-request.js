/* Material request: enter or paste lines, review, explicitly send. */
(function () {
  "use strict";
  let me = null;
  let req = null;          // saved request (null until first save)
  let lines = [];          // working lines
  let mode = "edit";       // edit | review | sent
  let justSentTo = null;
  let pendingConn = null;

  const $ = (id) => document.getElementById(id);

  function flash(kind, text) {
    $("err").hidden = kind !== "err";
    $("ok").hidden = kind !== "ok";
    if (kind) $(kind).textContent = text;
  }

  function blankLine() { return { raw_description: "", quantity: 1, uom: "", note: "", source_text: null }; }

  function readLines() {
    document.querySelectorAll("#lines .line").forEach((el, i) => {
      lines[i].raw_description = el.querySelector("[data-k=desc]").value;
      lines[i].quantity = el.querySelector("[data-k=qty]").value;
      lines[i].uom = el.querySelector("[data-k=uom]").value;
      lines[i].note = el.querySelector("[data-k=note]").value;
    });
  }

  function renderLines() {
    $("lines").innerHTML = lines.map((l, i) => `
      <div class="line">
        <div class="lnum"><span>#${i + 1}</span><button type="button" data-rm="${i}">${P.th("remove_line")}</button></div>
        <label class="field" style="margin:0"><span class="muted">${P.th("col_description")}</span>
          <input data-k="desc" type="text" maxlength="500" value="${P.esc(l.raw_description)}" /></label>
        <div class="row2">
          <label class="field" style="margin:0"><span class="muted">${P.th("col_qty")}</span>
            <input data-k="qty" type="number" inputmode="decimal" min="0" step="any" value="${P.esc(l.quantity)}" /></label>
          <label class="field" style="margin:0"><span class="muted">${P.th("col_unit")}</span>
            <input data-k="uom" type="text" maxlength="20" value="${P.esc(l.uom || "")}" /></label>
        </div>
        <label class="field" style="margin:0"><span class="muted">${P.th("col_note")}</span>
          <input data-k="note" type="text" maxlength="500" value="${P.esc(l.note || "")}" /></label>
        ${l.source_text ? `<div class="src">“${P.esc(l.source_text)}”</div>` : ""}
      </div>`).join("");
    document.querySelectorAll("[data-rm]").forEach((b) => b.addEventListener("click", () => {
      readLines();
      lines.splice(Number(b.dataset.rm), 1);
      if (!lines.length) lines.push(blankLine());
      renderLines();
    }));
  }

  function payload() {
    readLines();
    return {
      title: $("title").value,
      reference: $("reference").value,
      lines: lines.filter((l) => String(l.raw_description).trim() || l.note)
        .map((l) => ({ raw_description: l.raw_description, quantity: l.quantity, uom: l.uom, note: l.note, source_text: l.source_text })),
      visit_id: P.visitId(),
    };
  }

  async function save() {
    const body = payload();
    req = req
      ? await P.api("PATCH", "/api/pilot/contractor/requests/" + req.request_id, body)
      : await P.api("POST", "/api/pilot/contractor/requests", body);
    if (!new URLSearchParams(location.search).get("id")) {
      history.replaceState(null, "", "?id=" + encodeURIComponent(req.request_id));
    }
    lines = req.lines.map((l) => Object.assign({}, l));
    return req;
  }

  function renderReview() {
    const sent = req.status === "SENT";
    $("reviewHeading").textContent = sent
      ? (justSentTo ? P.t("sent_title", { supplier: justSentTo }) : req.title)
      : P.t("review_title");
    const routing = P.t(req.priority === "priority" ? "routing_priority" : "routing_standard");
    const review = sent ? (justSentTo ? P.t("sent_body", { supplier: justSentTo }) : P.t("sent_readonly")) : P.t("review_private");
    $("reviewSub").textContent = review + " " + routing;
    $("rvTitle").textContent = req.title;
    $("rvRef").textContent = req.reference || "";
    $("rvLines").innerHTML = req.lines.map((l) => `<tr><td>${l.line_no}</td><td>${P.esc(l.raw_description)}</td>
      <td class="q">${P.esc(P.fmtQty(l.quantity))}</td><td>${P.esc(l.uom || "")}</td><td>${P.esc(l.note || "")}</td></tr>`).join("");
    const area = $("sendArea");
    if (sent) {
      area.innerHTML = `<div class="card">${(req.sent_to || []).map((s) => {
        const v = { supplier: s.supplier_name };
        const k = s.status === "ACKNOWLEDGED" ? ["ack", "share_ack"] : s.status === "VIEWED" ? ["sent", "share_viewed"] : ["new", "share_new"];
        return `<p style="margin:0 0 6px"><span class="chip sent">${P.th("status_sent", v)}</span> <span class="chip ${k[0]}">${P.th(k[1], v)}</span>
          <span class="muted">${P.esc(P.fmtDate(s.sent_at))}</span></p>`;
      }).join("")}<a class="btn ghost small" href="/contractor-home.html" style="margin-top:8px">${P.th("dashboard")}</a></div>`;
      $("reviewActions").hidden = true;
      return;
    }
    $("reviewActions").hidden = false;
    const conns = me.connections || [];
    area.innerHTML = conns.length
      ? `<div class="btn-row" style="margin-bottom:12px">${conns.map((c) =>
          `<button class="btn primary" type="button" data-send="${P.esc(c.connection_id)}" data-name="${P.esc(c.supplier_name)}">${P.th("send_to", { supplier: c.supplier_name })}</button>`).join("")}</div>`
      : `<p class="note">${P.th("no_supplier_to_send")}</p>`;
    area.querySelectorAll("[data-send]").forEach((b) => b.addEventListener("click", () => openConfirm(b.dataset.send, b.dataset.name)));
  }

  function openConfirm(connectionId, name) {
    pendingConn = { id: connectionId, name };
    $("dlgTitle").textContent = P.t("confirm_title", { supplier: name });
    $("dlgBody").textContent = P.t("confirm_body", { supplier: name, n: req.lines.length });
    $("dlg").hidden = false;
    $("dlgOk").focus();
  }

  async function doSend() {
    const btn = $("dlgOk");
    btn.disabled = true;
    try {
      req = await P.api("POST", `/api/pilot/contractor/requests/${req.request_id}/send`,
        { connection_id: pendingConn.id, confirm: true, visit_id: P.visitId() });
      justSentTo = pendingConn.name;
      $("dlg").hidden = true;
      flash(null);
      show("review");
    } catch (err) {
      $("dlg").hidden = true;
      flash("err", P.errText(err));
    } finally {
      btn.disabled = false;
    }
  }

  function show(m) {
    mode = m;
    $("editView").hidden = m !== "edit";
    $("reviewView").hidden = m === "edit";
    if (m === "edit") {
      $("editTitle").textContent = req ? req.title : P.t("req_new_title");
      renderLines();
    } else {
      renderReview();
    }
    window.scrollTo(0, 0);
  }

  async function init() {
    P.mountLangSwitch(true);
    me = await P.me();
    if (!me) { location.replace("/contractor-account.html?mode=signin"); return; }
    if (me.account_type !== "contractor") { location.replace("/supplier-inbox.html"); return; }
    P.signedIn = true;
    P.adoptServerLang(me.language);
    const id = P.qs("id");
    if (id) {
      try { req = await P.api("GET", "/api/pilot/contractor/requests/" + encodeURIComponent(id)); }
      catch (e) { location.replace("/contractor-home.html"); return; }
      $("title").value = req.title;
      $("reference").value = req.reference || "";
      lines = req.lines.map((l) => Object.assign({}, l));
    } else {
      lines = [blankLine()];
      P.event("material_request_started", "contractor-request");
    }
    $("main").hidden = false;
    show(req && req.status === "SENT" ? "review" : "edit");
    P.onLang = () => show(mode);

    $("addLine").addEventListener("click", () => { readLines(); lines.push(blankLine()); renderLines(); });
    $("pasteAdd").addEventListener("click", async () => {
      readLines();
      try {
        const res = await P.api("POST", "/api/pilot/contractor/lines/parse", { text: $("paste").value });
        lines = lines.filter((l) => String(l.raw_description).trim()).concat(res.lines);
        $("paste").value = "";
        $("pasteBox").open = false;
        renderLines();
        flash(null);
      } catch (err) { flash("err", P.errText(err)); }
    });
    $("saveDraft").addEventListener("click", async () => {
      try { await save(); show("edit"); flash("ok", P.t("saved")); } catch (err) { flash("err", P.errText(err)); }
    });
    $("toReview").addEventListener("click", async () => {
      try { await save(); flash(null); show("review"); } catch (err) { flash("err", P.errText(err)); }
    });
    $("backToEdit").addEventListener("click", () => show("edit"));
    $("dlgCancel").addEventListener("click", () => { $("dlg").hidden = true; });
    $("dlgOk").addEventListener("click", doSend);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("dlg").hidden = true; });
  }
  document.addEventListener("DOMContentLoaded", init);
})();
