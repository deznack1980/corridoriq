"""Contractor material requests (private) and the explicit send to a supplier.

CONTRACTOR-CONFIDENTIAL: requests and lines live in the contractor's own
tenant store and are reachable only through that contractor's context.
TRANSACTIONAL: "send" copies one request (title, reference, lines) into the
platform exchange for exactly one connected supplier. The supplier reads only
that snapshot — never the contractor's store, other requests, or any other
contractor data.

Contractor text is kept exactly as entered. Nothing is matched to products,
catalogs, prices or stock.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from datetime import datetime, timezone

from pipeline.pilot import accounts, analytics
from pipeline.pilot.accounts import PilotError, Principal

MAX_LINES = 300
MAX_DESC = 500
MAX_NOTE = 500
MAX_TITLE = 120
MAX_REF = 200
MAX_UOM = 20
MAX_PASTE = 60_000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS material_requests (
    request_id  TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    created_by  INTEGER NOT NULL,
    title       TEXT NOT NULL,
    reference   TEXT,
    status      TEXT NOT NULL CHECK (status IN ('DRAFT', 'SENT')),
    -- Frozen at creation from request_priority(billing organization for the session email). Not a service level.
    priority    TEXT NOT NULL DEFAULT 'standard' CHECK (priority IN ('priority', 'standard')),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS material_request_lines (
    request_id      TEXT NOT NULL REFERENCES material_requests(request_id),
    tenant_id       TEXT NOT NULL,
    line_no         INTEGER NOT NULL,
    raw_description TEXT NOT NULL,
    quantity        REAL NOT NULL,
    uom             TEXT,
    note            TEXT,
    source_text     TEXT,
    PRIMARY KEY (request_id, line_no)
);
"""

_REQ_ID = re.compile(r"^r-[0-9a-f]{16}$")
_SHARE_ID = re.compile(r"^s-[0-9a-f]{16}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _store(platform, principal: Principal):
    accounts.require_contractor(principal)
    store = platform.contractors.store(principal.ctx)
    store.ensure_schema(_SCHEMA)
    with store.connect() as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(material_requests)")}
        if cols and "priority" not in cols:
            conn.execute("ALTER TABLE material_requests ADD COLUMN priority TEXT NOT NULL DEFAULT 'standard'")
            conn.commit()
    return store


# Billing lookup may read only these tables. Supplier intelligence stays closed.
_BILLING_READ_TABLES = {"users", "organizations", "billing_accounts", "sqlite_master", "sqlite_schema"}


def _open_billing_lookup():
    """Read-only view of the application database for the priority linkage.

    The pilot session stays in the pilot database. This connection is used only
    to find the contractor organization that owns the session email.
    """
    from pipeline.api import server as server_mod

    conn = server_mod._factory()
    conn.row_factory = sqlite3.Row

    def _authorizer(action, arg1, arg2, db_name, source):
        if action not in (20, 21, 22, 31):
            return 1
        if action == 20 and arg1 not in _BILLING_READ_TABLES and arg1 not in (None, ""):
            return 1
        return 0

    conn.set_authorizer(_authorizer)
    return conn


def _billing_organization_id(principal: Principal):
    """The Contractor Pro organization for this session email, or None.

    Exactly one active user in one active contractor organization matches.
    Zero matches and more than one match both fail closed. The pilot-platform
    organization is never treated as a billing organization. Client input is
    not consulted.
    """
    from pipeline.auth.service import normalize_email

    if principal is None or principal.kind != "contractor":
        return None, None
    user = principal.user or {}
    email = normalize_email(user.get("email") or "") if isinstance(user, dict) else ""
    if "@" not in email:
        return None, None
    try:
        conn = _open_billing_lookup()
    except Exception:
        return None, None
    try:
        rows = conn.execute(
            """
            SELECT u.organization_id
            FROM users u
            JOIN organizations o ON o.id = u.organization_id
            WHERE u.normalized_email = ?
              AND u.is_active = 1
              AND o.is_active = 1
              AND o.account_type = 'contractor'
            """,
            (email,),
        ).fetchall()
    except Exception:
        conn.close()
        return None, None
    if len(rows) != 1:
        conn.close()
        return None, None
    return int(rows[0]["organization_id"]), conn


def _routing_class(pconn, principal: Principal) -> str:
    """Classify from the billing organization that owns the session email.

    ``pconn`` is the pilot database and is intentionally not the source of the
    organization. A body field cannot change the result.
    """
    from pipeline.billing.entitlements import PRIORITY, STANDARD, request_priority

    del pconn  # pilot-platform is not a Contractor Pro organization
    org_id, billing = _billing_organization_id(principal)
    if org_id is None or billing is None:
        return STANDARD
    try:
        value = request_priority(billing, org_id)
    except Exception:
        return STANDARD
    finally:
        billing.close()
    return value if value in (PRIORITY, STANDARD) else STANDARD


# ---- line validation / paste parsing ---------------------------------------------------

def _text(value, limit, *, required=False, code="invalid_line"):
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise PilotError(code)
    value = value.replace("\r", "").strip("\n")
    if "\x00" in value or len(value) > limit:
        raise PilotError(code)
    if required and not value.strip():
        raise PilotError(code)
    return value


def _quantity(value) -> float:
    try:
        q = float(str(value).strip())
    except (TypeError, ValueError):
        raise PilotError("invalid_quantity")
    if not (q > 0 and q <= 1_000_000) or q != q:
        raise PilotError("invalid_quantity")
    return round(q, 4)


def clean_lines(raw_lines) -> list[dict]:
    if not isinstance(raw_lines, list) or not raw_lines:
        raise PilotError("lines_required")
    if len(raw_lines) > MAX_LINES:
        raise PilotError("too_many_lines")
    out = []
    for i, ln in enumerate(raw_lines, start=1):
        if not isinstance(ln, dict):
            raise PilotError("invalid_line")
        out.append({
            "line_no": i,
            "raw_description": _text(ln.get("raw_description"), MAX_DESC, required=True),
            "quantity": _quantity(ln.get("quantity")),
            "uom": _text(ln.get("uom"), MAX_UOM).strip() or None,
            "note": _text(ln.get("note"), MAX_NOTE).strip() or None,
            "source_text": _text(ln.get("source_text"), MAX_DESC + 40) or None,
        })
    return out


_LEADING_QTY = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s+(?:x\s+|X\s+)?(?:([A-Za-z]{1,6}\.?)\s+(?=\S))?(.+)$")
_FRACTION_START = re.compile(r"^\d+\s*/\s*\d+")
_TRAILING_QTY = re.compile(r"^(.+?)\s+(?:x|X|qty:?|Qty:?|QTY:?)\s*(\d+(?:[.,]\d+)?)\s*$")
_UNITS = {"ea", "each", "pc", "pcs", "ft", "lf", "box", "bx", "bag", "roll", "cs", "case", "pk", "pkg",
          "gal", "lb", "lbs", "set", "pr", "pair", "coil", "len", "stick", "m", "pza", "pzas", "caja",
          "rollo", "bolsa", "tramo", "juego"}


def parse_pasted(text) -> list[dict]:
    """Split pasted text into editable lines for review. Each line keeps the
    exact pasted text in ``source_text``. A leading quantity ("12 ea 1/2in
    elbow") or trailing quantity ("1/2in elbow x 12") is suggested; otherwise
    quantity 1. Tab/comma separated "description, qty, unit" rows are also
    read. Nothing else is inferred."""
    if not isinstance(text, str) or len(text) > MAX_PASTE:
        raise PilotError("invalid_paste")
    rows = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        desc, qty, uom = line, 1.0, None
        cells = [c.strip() for c in (line.split("\t") if "\t" in line else line.split(","))]
        if len(cells) in (2, 3) and re.fullmatch(r"\d+(?:\.\d+)?", cells[1] or "") and cells[0]:
            desc, qty = cells[0], float(cells[1])
            uom = cells[2] if len(cells) == 3 and cells[2] else None
        else:
            m = _LEADING_QTY.match(line)
            t = _TRAILING_QTY.match(line)
            if m and _FRACTION_START.match(line[m.end(1):].strip()):
                m = None  # "2 1/2in …" is ambiguous (two-and-a-half inch?) — don't guess
            if m and float(m.group(1).replace(",", ".")) > 0:
                unit = (m.group(2) or "").rstrip(".")
                if unit and unit.lower() in _UNITS:
                    desc, qty, uom = m.group(3).strip(), float(m.group(1).replace(",", ".")), unit
                else:
                    rest = ((m.group(2) + " ") if m.group(2) else "") + m.group(3)
                    desc, qty = rest.strip(), float(m.group(1).replace(",", "."))
            elif t and float(t.group(2).replace(",", ".")) > 0:
                desc, qty = t.group(1).strip(), float(t.group(2).replace(",", "."))
        rows.append({"raw_description": desc[:MAX_DESC], "quantity": qty if 0 < qty <= 1_000_000 else 1.0,
                     "uom": uom, "note": None, "source_text": line[:MAX_DESC + 40]})
        if len(rows) > MAX_LINES:
            raise PilotError("too_many_lines")
    if not rows:
        raise PilotError("lines_required")
    return rows


# ---- contractor requests -----------------------------------------------------------

def _req_row(conn, request_id):
    if not isinstance(request_id, str) or not _REQ_ID.fullmatch(request_id):
        raise PilotError("not_found", status=404)
    row = conn.execute("SELECT * FROM material_requests WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        raise PilotError("not_found", status=404)
    return row


def _serialize(conn, row, shares) -> dict:
    lines = [dict(r) for r in conn.execute(
        "SELECT line_no, raw_description, quantity, uom, note, source_text FROM material_request_lines "
        "WHERE request_id=? ORDER BY line_no", (row["request_id"],))]
    return {"request_id": row["request_id"], "title": row["title"], "reference": row["reference"],
            "status": row["status"], "priority": row["priority"] if "priority" in row.keys() else "standard",
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "lines": lines, "sent_to": shares.get(row["request_id"], [])}


def _shares_by_request(pconn, platform, principal) -> dict:
    out: dict = {}
    for r in pconn.execute("SELECT request_id, supplier_tenant_id, sent_at, status FROM request_shares "
                           "WHERE contractor_tenant_id=? ORDER BY sent_at", (principal.tenant_id,)):
        try:
            name = platform.suppliers.get(r["supplier_tenant_id"]).display_name
        except Exception:
            name = None
        out.setdefault(r["request_id"], []).append(
            {"supplier_name": name, "sent_at": r["sent_at"], "status": r["status"]})
    return out


def list_requests(pconn, platform, principal) -> list[dict]:
    store = _store(platform, principal)
    shares = _shares_by_request(pconn, platform, principal)
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM material_requests ORDER BY updated_at DESC").fetchall()
        return [{"request_id": r["request_id"], "title": r["title"], "reference": r["reference"],
                 "status": r["status"], "priority": r["priority"] if "priority" in r.keys() else "standard",
                 "updated_at": r["updated_at"],
                 "line_count": conn.execute("SELECT COUNT(*) FROM material_request_lines WHERE request_id=?",
                                            (r["request_id"],)).fetchone()[0],
                 "sent_to": shares.get(r["request_id"], [])} for r in rows]


def get_request(pconn, platform, principal, request_id) -> dict:
    store = _store(platform, principal)
    with store.connect() as conn:
        return _serialize(conn, _req_row(conn, request_id), _shares_by_request(pconn, platform, principal))


def _write_lines(conn, tenant_id, request_id, lines):
    conn.execute("DELETE FROM material_request_lines WHERE request_id=?", (request_id,))
    for ln in lines:
        conn.execute("INSERT INTO material_request_lines (request_id, tenant_id, line_no, raw_description, quantity, "
                     "uom, note, source_text) VALUES (?,?,?,?,?,?,?,?)",
                     (request_id, tenant_id, ln["line_no"], ln["raw_description"], ln["quantity"], ln["uom"],
                      ln["note"], ln["source_text"]))


def create_request(pconn, platform, principal, body: dict) -> dict:
    store = _store(platform, principal)
    title = _text(body.get("title"), MAX_TITLE, required=True, code="title_required").strip()
    reference = _text(body.get("reference"), MAX_REF, code="invalid_reference").strip() or None
    lines = clean_lines(body.get("lines"))
    request_id = "r-" + secrets.token_hex(8)
    now = _now()
    # Body keys such as priority, plan, entitlement, account_type and organization are not read.
    priority = _routing_class(pconn, principal)
    with store.transaction() as conn:
        conn.execute("INSERT INTO material_requests (request_id, tenant_id, created_by, title, reference, status, "
                     "priority, created_at, updated_at) VALUES (?,?,?,?,?, 'DRAFT', ?, ?, ?)",
                     (request_id, store.tenant_id, principal.user_id, title, reference, priority, now, now))
        _write_lines(conn, store.tenant_id, request_id, lines)
    analytics.record_for_contractor(pconn, "material_request_created",
                                    accounts.contractor_profile(pconn, principal.tenant_id),
                                    user_id=principal.user_id, visit_id=body.get("visit_id"),
                                    page="contractor-request")
    return get_request(pconn, platform, principal, request_id)


def update_request(pconn, platform, principal, request_id, body: dict) -> dict:
    store = _store(platform, principal)
    title = _text(body.get("title"), MAX_TITLE, required=True, code="title_required").strip()
    reference = _text(body.get("reference"), MAX_REF, code="invalid_reference").strip() or None
    lines = clean_lines(body.get("lines"))
    with store.transaction() as conn:
        row = _req_row(conn, request_id)
        if row["status"] != "DRAFT":
            raise PilotError("already_sent", status=409)
        conn.execute("UPDATE material_requests SET title=?, reference=?, updated_at=? WHERE request_id=?",
                     (title, reference, _now(), request_id))
        _write_lines(conn, store.tenant_id, request_id, lines)
    return get_request(pconn, platform, principal, request_id)


def send_request(pconn, platform, principal, request_id, body: dict) -> dict:
    """Explicit, confirmed share of one request with one connected supplier."""
    if body.get("confirm") is not True:
        raise PilotError("confirmation_required")
    store = _store(platform, principal)
    supplier_tenant_id, supplier_name = accounts.connection_supplier(pconn, platform, principal,
                                                                     body.get("connection_id"))
    profile = accounts.contractor_profile(pconn, principal.tenant_id)
    with store.connect() as conn:
        row = _req_row(conn, request_id)
        if row["status"] != "DRAFT":
            raise PilotError("already_sent", status=409)
        lines = conn.execute("SELECT * FROM material_request_lines WHERE request_id=? ORDER BY line_no",
                             (request_id,)).fetchall()
    if not lines:
        raise PilotError("lines_required")
    share_id = "s-" + secrets.token_hex(8)
    now = _now()
    try:
        priority = row["priority"] if "priority" in row.keys() else "standard"
        pconn.execute(
            "INSERT INTO request_shares (share_id, request_id, contractor_tenant_id, supplier_tenant_id, connection_id, "
            "sender_user_id, sent_at, status, contractor_name, title, reference, priority) "
            "VALUES (?,?,?,?,?,?,?, 'NEW', ?,?,?,?)",
            (share_id, request_id, principal.tenant_id, supplier_tenant_id, body.get("connection_id"),
             principal.user_id, now, profile["business_name"], row["title"], row["reference"], priority))
        for ln in lines:
            pconn.execute("INSERT INTO request_share_lines (share_id, line_no, raw_description, quantity, uom, note) "
                          "VALUES (?,?,?,?,?,?)",
                          (share_id, ln["line_no"], ln["raw_description"], ln["quantity"], ln["uom"], ln["note"]))
        analytics.record_for_contractor(pconn, "material_request_sent", profile, user_id=principal.user_id,
                                        visit_id=body.get("visit_id"), page="contractor-request", commit=False)
        pconn.commit()
    except Exception as exc:
        pconn.rollback()
        if "UNIQUE" in str(exc):
            raise PilotError("already_sent", status=409)
        raise
    try:
        with store.transaction() as conn:
            conn.execute("UPDATE material_requests SET status='SENT', updated_at=? WHERE request_id=?",
                         (now, request_id))
    except Exception:
        # Keep both sides consistent: withdraw the share if the contractor
        # record could not be marked as sent.
        pconn.execute("DELETE FROM request_share_lines WHERE share_id=?", (share_id,))
        pconn.execute("DELETE FROM request_shares WHERE share_id=?", (share_id,))
        pconn.commit()
        raise
    from pipeline.auth.service import write_audit
    write_audit(pconn, event_type="pilot_request_sent", success=True, user_id=principal.user_id,
                resource_type="request_share", resource_id=share_id,
                details={"request_id": request_id, "supplier_tenant_id": supplier_tenant_id})
    out = get_request(pconn, platform, principal, request_id)
    out["sent_to_supplier"] = supplier_name
    return out


# ---- supplier inbox --------------------------------------------------------------

def _share_out(r) -> dict:
    priority = r["priority"] if "priority" in r.keys() else "standard"
    return {"share_id": r["share_id"], "contractor_name": r["contractor_name"], "title": r["title"],
            "reference": r["reference"], "sent_at": r["sent_at"], "status": r["status"],
            "priority": priority if priority in ("priority", "standard") else "standard",
            "viewed_at": r["viewed_at"], "acknowledged_at": r["acknowledged_at"]}


def inbox(pconn, principal) -> list[dict]:
    accounts.require_supplier(principal)
    rows = pconn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM request_share_lines l WHERE l.share_id=s.share_id) AS n "
        "FROM request_shares s WHERE supplier_tenant_id=? "
        "ORDER BY CASE WHEN s.priority='priority' THEN 0 ELSE 1 END, s.sent_at ASC, s.share_id ASC",
        (principal.tenant_id,),
    ).fetchall()
    return [dict(_share_out(r), line_count=r["n"]) for r in rows]


def _supplier_share(pconn, principal, share_id):
    accounts.require_supplier(principal)
    if not isinstance(share_id, str) or not _SHARE_ID.fullmatch(share_id):
        raise PilotError("not_found", status=404)
    row = pconn.execute("SELECT * FROM request_shares WHERE share_id=? AND supplier_tenant_id=?",
                        (share_id, principal.tenant_id)).fetchone()
    if row is None:
        raise PilotError("not_found", status=404)
    return row


def inbox_detail(pconn, principal, share_id) -> dict:
    row = _supplier_share(pconn, principal, share_id)
    if row["status"] == "NEW":
        pconn.execute("UPDATE request_shares SET status='VIEWED', viewed_at=?, viewed_by=? "
                      "WHERE share_id=? AND status='NEW'", (_now(), principal.user_id, share_id))
        pconn.commit()
        row = _supplier_share(pconn, principal, share_id)
    lines = [dict(r) for r in pconn.execute(
        "SELECT line_no, raw_description, quantity, uom, note FROM request_share_lines WHERE share_id=? "
        "ORDER BY line_no", (share_id,))]
    return dict(_share_out(row), lines=lines)


def acknowledge(pconn, principal, share_id) -> dict:
    row = _supplier_share(pconn, principal, share_id)
    if row["status"] != "ACKNOWLEDGED":
        now = _now()
        pconn.execute("UPDATE request_shares SET status='ACKNOWLEDGED', acknowledged_at=?, acknowledged_by=?, "
                      "viewed_at=COALESCE(viewed_at, ?), viewed_by=COALESCE(viewed_by, ?) WHERE share_id=?",
                      (now, principal.user_id, now, principal.user_id, share_id))
        pconn.commit()
        from pipeline.auth.service import write_audit
        write_audit(pconn, event_type="pilot_request_acknowledged", success=True, user_id=principal.user_id,
                    resource_type="request_share", resource_id=share_id)
    return inbox_detail(pconn, principal, share_id)
