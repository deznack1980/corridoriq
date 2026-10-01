"""Apply human review decisions from a completed review CSV.

Reviewers fill the ``decision`` column of review.csv with CONFIRM, REJECT or
SET_COMPANY (plus ``decided_company_id`` for SET_COMPANY). Decisions are stored
in the tenant's own review_decisions table and override automated matches in
later classifications. Every row is validated against this tenant's accounts
and the shared company index; invalid rows are reported, never guessed.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from pipeline.book_match.report import unescape_cell
from pipeline.book_match.schema import BOOK_SCHEMA

log = logging.getLogger("corridoriq.book_match")
DECISIONS = ("CONFIRM", "REJECT", "SET_COMPANY")


@dataclass
class ReviewResult:
    applied: int = 0
    skipped_blank: int = 0
    issues: list = field(default_factory=list)  # (row_number, reason)


def _int_or_none(v):
    v = (v or "").strip()
    return int(v) if v.isdigit() else None


def apply_review_csv(store, review_csv, index, *, default_reviewer: str = "unspecified") -> ReviewResult:
    path = Path(review_csv)
    if not path.is_file():
        raise FileNotFoundError("review file not found")
    text = path.read_bytes().decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or not {"supplier_account_id", "decision"} <= set(reader.fieldnames):
        raise ValueError("review file needs supplier_account_id and decision columns")
    store.ensure_schema(BOOK_SCHEMA)
    tid = store.tenant_id
    res = ReviewResult()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with store.transaction() as conn:
        known = {r["supplier_account_id"] for r in conn.execute(
            "SELECT supplier_account_id FROM book_accounts WHERE tenant_id=?", (tid,))}
        for i, row in enumerate(reader, start=2):
            decision = unescape_cell(row.get("decision") or "").strip().upper()
            if not decision:
                res.skipped_blank += 1
                continue
            acct = unescape_cell(row.get("supplier_account_id") or "").strip()
            if decision not in DECISIONS:
                res.issues.append((i, "unknown decision"))
                continue
            if acct not in known:
                res.issues.append((i, "account not in this tenant's book"))
                continue
            company = None
            if decision == "CONFIRM":
                company = _int_or_none(row.get("decided_company_id")) or _int_or_none(row.get("candidate_company_id"))
            elif decision == "SET_COMPANY":
                company = _int_or_none(row.get("decided_company_id"))
            if decision != "REJECT" and (company is None or index.get(company) is None):
                res.issues.append((i, "company ID missing or not an active CorridorIQ company"))
                continue
            reviewer = unescape_cell(row.get("reviewer") or "").strip()[:80] or default_reviewer
            note = unescape_cell(row.get("note") or "").strip()[:500] or None
            conn.execute(
                "INSERT INTO review_decisions (tenant_id, supplier_account_id, decision, company_id, reviewer, "
                "note, source_run_id, decided_at) VALUES (?,?,?,?,?,?,?,?)",
                (tid, acct, decision, company, reviewer, note,
                 unescape_cell(row.get("run_id") or "").strip()[:64] or None, now))
            res.applied += 1
    log.info("book_match review tenant=%s applied=%d skipped=%d issues=%d",
             tid, res.applied, res.skipped_blank, len(res.issues))
    return res
