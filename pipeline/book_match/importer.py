"""Customer-book CSV import into one supplier tenant's store.

Guarantees:
  * all-or-nothing per file (one transaction; any failure rolls back);
  * every row is accounted for: accepted, rejected (with reason), or accepted
    with warnings — nothing is dropped silently;
  * the supplier's values are stored as provided (trimmed); matching keys are
    stored separately;
  * re-importing a byte-identical file is a no-op that returns the original
    batch; each import is a full snapshot of the book;
  * logs carry tenant ID, batch ID, counts and timing only — never row values.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from pipeline.book_match import normalize as N
from pipeline.book_match.schema import BOOK_SCHEMA

log = logging.getLogger("corridoriq.book_match")

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ROWS = 250_000
MAX_FIELD_CHARS = 300
_ID_MAX = 64

# Canonical field -> accepted header spellings (case/space/punct-insensitive).
HEADER_ALIASES = {
    "supplier_account_id": ("supplier_account_id", "account_id", "account", "account_number", "acct",
                            "acct_no", "customer_id", "customer_number", "cust_id", "cust_no"),
    "company_name": ("company_name", "company", "name", "customer_name", "account_name", "legal_name"),
    "dba_name": ("dba_name", "dba", "doing_business_as", "trade_name"),
    "address": ("address", "address_line_1", "street", "street_address", "address1"),
    "city": ("city",),
    "state": ("state", "st"),
    "postal_code": ("postal_code", "zip", "zip_code", "zipcode", "postal"),
    "phone": ("phone", "phone_number", "main_phone", "telephone"),
    "roc_license": ("roc_license", "roc", "license", "license_number", "roc_number"),
    "branch": ("branch", "branch_name", "location", "home_branch"),
    "assigned_rep": ("assigned_rep", "rep", "sales_rep", "salesperson", "account_rep"),
    "last_purchase_date": ("last_purchase_date", "last_purchase", "last_order_date", "last_sale_date",
                           "last_invoice_date"),
}
REQUIRED = ("supplier_account_id", "company_name")


class BookImportError(Exception):
    """The file as a whole cannot be imported. Nothing was written."""


@dataclass
class ImportResult:
    batch_id: str
    already_imported: bool
    row_count: int
    accepted: int
    rejected: int
    warnings: int
    unknown_columns: list = field(default_factory=list)
    seconds: float = 0.0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canon(header: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in (header or "").strip().lower()).strip("_")


def _map_headers(headers: list[str]) -> tuple[dict, list]:
    lookup = {}
    for field_name, aliases in HEADER_ALIASES.items():
        for a in aliases:
            lookup[_canon(a)] = field_name
    mapping, unknown, seen = {}, [], set()
    for idx, h in enumerate(headers):
        f = lookup.get(_canon(h))
        if f is None:
            unknown.append((h or "").strip()[:40])
        elif f in seen:
            raise BookImportError(f"two columns map to the same field: {f}")
        else:
            mapping[idx] = f
            seen.add(f)
    missing = [r for r in REQUIRED if r not in seen]
    if missing:
        raise BookImportError(f"missing required column(s): {', '.join(missing)}")
    return mapping, unknown


def preview(field_name: str, value: str | None) -> str:
    """Safe representation for issue reports: field, first two characters, length."""
    if value is None or value == "":
        return f"{field_name}: <empty>"
    v = str(value)
    head = "".join(ch if ch.isprintable() else "?" for ch in v[:2])
    return f"{field_name}: '{head}…' (len {len(v)})"


def parse_purchase_date(raw: str | None, today: date) -> tuple[str | None, str | None]:
    """ISO (YYYY-MM-DD, optionally with time) or US M/D/YYYY. Returns
    (iso_date_or_None, warning_or_None). Two-digit years are not guessed."""
    if not raw:
        return None, None
    s = raw.strip()
    parsed = None
    try:
        parsed = date.fromisoformat(s[:10])
    except ValueError:
        parts = s.replace("-", "/").split("/")
        if len(parts) == 3 and all(p.isdigit() for p in parts) and len(parts[2]) == 4:
            try:
                parsed = date(int(parts[2]), int(parts[0]), int(parts[1]))
            except ValueError:
                parsed = None
    if parsed is None:
        return None, "unparseable_last_purchase_date"
    if parsed > today:
        return None, "future_last_purchase_date"
    if parsed.year < 1950:
        return None, "implausible_last_purchase_date"
    return parsed.isoformat(), None


def _read_rows(path: Path) -> tuple[list[str], list[list[str]], bytes]:
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise BookImportError("file exceeds the 50 MB import limit")
    data = path.read_bytes()
    if b"\x00" in data:
        raise BookImportError("file contains NUL bytes; not a text CSV")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise BookImportError("file is not UTF-8 encoded") from exc
    csv.field_size_limit(100_000)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        rows = list(reader)
    except csv.Error as exc:
        raise BookImportError(f"malformed CSV: {exc}") from exc
    if not rows:
        raise BookImportError("file is empty")
    header, body = rows[0], rows[1:]
    if len(body) > MAX_ROWS:
        raise BookImportError("file exceeds the 250,000 row import limit")
    return header, body, data


def _validate_row(values: dict, today: date) -> tuple[str | None, list[tuple[str, str]], dict]:
    """Return (reject_reason, warnings[(reason, preview)], cleaned)."""
    # Quoted multi-line values (e.g. two-line addresses) become one line.
    cleaned = {k: (" ".join(v.replace("\r", "\n").split("\n")).strip() if isinstance(v, str) else v)
               for k, v in values.items()}
    cleaned = {k: (v if v != "" else None) for k, v in cleaned.items()}
    for k, v in cleaned.items():
        if v is not None and len(v) > MAX_FIELD_CHARS:
            return "field_too_long", [], {"_field": k, "_value": v}
        if v is not None and any(ord(ch) < 32 and ch not in "\t" for ch in v):
            return "control_characters", [], {"_field": k, "_value": v}
    acct = cleaned.get("supplier_account_id")
    if not acct:
        return "missing_account_id", [], {"_field": "supplier_account_id", "_value": acct}
    if len(acct) > _ID_MAX:
        return "account_id_too_long", [], {"_field": "supplier_account_id", "_value": acct}
    if not cleaned.get("company_name"):
        return "missing_company_name", [], {"_field": "company_name", "_value": None}
    warnings = []
    iso, warn = parse_purchase_date(cleaned.get("last_purchase_date"), today)
    if warn:
        warnings.append((warn, preview("last_purchase_date", cleaned.get("last_purchase_date"))))
    cleaned["_last_purchase_on"] = iso
    if cleaned.get("phone") and not N.phone_key(cleaned["phone"]):
        warnings.append(("unusable_phone", preview("phone", cleaned["phone"])))
    st = cleaned.get("state")
    if st and not (len(st) == 2 and st.isalpha()):
        warnings.append(("nonstandard_state", preview("state", st)))
    if cleaned.get("roc_license") and not N.license_key(cleaned["roc_license"]):
        warnings.append(("unusable_roc_license", preview("roc_license", cleaned["roc_license"])))
    return None, warnings, cleaned


def import_book(store, csv_path, *, today: date | None = None) -> ImportResult:
    """Import one CSV into the store bound to one tenant. Raises BookImportError
    (nothing written) when the file as a whole is unusable."""
    started = time.perf_counter()
    path = Path(csv_path)
    if not path.is_file():
        raise BookImportError("input file not found")
    today = today or date.today()
    header, body, data = _read_rows(path)
    sha = hashlib.sha256(data).hexdigest()
    mapping, unknown = _map_headers(header)
    store.ensure_schema(BOOK_SCHEMA)
    tid = store.tenant_id

    with store.connect() as conn:
        prior = conn.execute(
            "SELECT batch_id, row_count, accepted_count, rejected_count, warning_count "
            "FROM import_batches WHERE source_sha256=? AND tenant_id=?", (sha, tid)).fetchone()
    if prior is not None:
        log.info("book_match import tenant=%s batch=%s skipped=identical_file", tid, prior["batch_id"])
        return ImportResult(prior["batch_id"], True, prior["row_count"], prior["accepted_count"],
                            prior["rejected_count"], prior["warning_count"], unknown,
                            time.perf_counter() - started)

    # Parse every row first; decide duplicates across the whole file.
    parsed = []
    id_rows: dict[str, list[int]] = {}
    for i, raw in enumerate(body, start=2):  # row 1 is the header
        values = {f: (raw[idx] if idx < len(raw) else "") for idx, f in mapping.items()}
        reason, warnings, cleaned = _validate_row(values, today)
        if len(raw) > len(header):
            warnings = warnings + [("extra_columns_ignored", f"{len(raw) - len(header)} extra value(s)")]
        parsed.append((i, reason, warnings, cleaned))
        if reason is None:
            id_rows.setdefault(cleaned["supplier_account_id"], []).append(i)
    duplicated = {k for k, rows in id_rows.items() if len(rows) > 1}

    batch_id = uuid.uuid4().hex
    now = _now()
    accepted = rejected = warned = 0
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO import_batches (batch_id, tenant_id, source_filename, source_sha256, source_bytes, "
            "imported_at, row_count, accepted_count, rejected_count, warning_count, unknown_columns) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (batch_id, tid, path.name[:200], sha, len(data), now, len(body), 0, 0, 0,
             ",".join(unknown) or None))
        conn.execute("UPDATE book_accounts SET in_latest_batch=0 WHERE tenant_id=?", (tid,))
        for row_no, reason, warnings, cleaned in parsed:
            if reason is None and cleaned["supplier_account_id"] in duplicated:
                reason = "duplicate_account_id_in_file"
                cleaned = dict(cleaned, _field="supplier_account_id", _value=cleaned["supplier_account_id"])
            if reason is not None:
                rejected += 1
                conn.execute(
                    "INSERT INTO import_issues (tenant_id, batch_id, row_number, level, reason, field_preview) "
                    "VALUES (?,?,?,?,?,?)",
                    (tid, batch_id, row_no, "REJECTED", reason,
                     preview(cleaned.get("_field") or "row", cleaned.get("_value"))))
                continue
            accepted += 1
            for w_reason, w_preview in warnings:
                warned += 1
                conn.execute(
                    "INSERT INTO import_issues (tenant_id, batch_id, row_number, level, reason, field_preview) "
                    "VALUES (?,?,?,?,?,?)", (tid, batch_id, row_no, "WARNING", w_reason, w_preview))
            _upsert_account(conn, tid, batch_id, row_no, cleaned, now)
        conn.execute(
            "UPDATE import_batches SET accepted_count=?, rejected_count=?, warning_count=? WHERE batch_id=?",
            (accepted, rejected, warned, batch_id))
    elapsed = time.perf_counter() - started
    log.info("book_match import tenant=%s batch=%s rows=%d accepted=%d rejected=%d warnings=%d seconds=%.2f",
             tid, batch_id, len(body), accepted, rejected, warned, elapsed)
    return ImportResult(batch_id, False, len(body), accepted, rejected, warned, unknown, elapsed)


def _upsert_account(conn, tid, batch_id, row_no, c, now):
    acct = c["supplier_account_id"]
    conn.execute(
        """INSERT INTO book_accounts (supplier_account_id, tenant_id, company_name, dba_name, address, city,
               state, postal_code, phone, roc_license, branch, assigned_rep, last_purchase_date,
               last_purchase_on, import_batch_id, source_row_number, in_latest_batch, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
           ON CONFLICT(supplier_account_id) DO UPDATE SET
               company_name=excluded.company_name, dba_name=excluded.dba_name, address=excluded.address,
               city=excluded.city, state=excluded.state, postal_code=excluded.postal_code,
               phone=excluded.phone, roc_license=excluded.roc_license, branch=excluded.branch,
               assigned_rep=excluded.assigned_rep, last_purchase_date=excluded.last_purchase_date,
               last_purchase_on=excluded.last_purchase_on, import_batch_id=excluded.import_batch_id,
               source_row_number=excluded.source_row_number, in_latest_batch=1, updated_at=excluded.updated_at
           WHERE book_accounts.tenant_id=excluded.tenant_id""",
        (acct, tid, c.get("company_name"), c.get("dba_name"), c.get("address"), c.get("city"),
         c.get("state"), c.get("postal_code"), c.get("phone"), c.get("roc_license"), c.get("branch"),
         c.get("assigned_rep"), c.get("last_purchase_date"), c.get("_last_purchase_on"),
         batch_id, row_no, now, now))
    addr = N.address_parts(street=c.get("address"), city=c.get("city"), postal=c.get("postal_code"))
    conn.execute(
        """INSERT INTO account_normalized (supplier_account_id, tenant_id, name_key, dba_key, phone_key,
               license_key, street_key, zip5, city_key, is_person_name, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(supplier_account_id) DO UPDATE SET
               name_key=excluded.name_key, dba_key=excluded.dba_key, phone_key=excluded.phone_key,
               license_key=excluded.license_key, street_key=excluded.street_key, zip5=excluded.zip5,
               city_key=excluded.city_key, is_person_name=excluded.is_person_name, updated_at=excluded.updated_at""",
        (acct, tid, N.name_key(c.get("company_name")) or None, N.name_key(c.get("dba_name")) or None,
         N.phone_key(c.get("phone")), N.license_key(c.get("roc_license")), addr["street"], addr["zip5"],
         addr["city"], 1 if N.person_name(c.get("company_name")) else 0, now))
