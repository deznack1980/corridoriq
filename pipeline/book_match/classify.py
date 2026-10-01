"""Book × Market classification and net-new candidates for one match run.

Purchase status comes only from the supplier's own last-purchase date and the
supplier's thresholds; market status comes only from CorridorIQ permit
activity. Neither is guessed:
  * no purchase date            → UNKNOWN_PURCHASE_STATUS
  * "former" requires the supplier-defined former_after_days threshold
  * unconfirmed identity        → IDENTITY_REVIEW (never classified on a guess)

Human review decisions (latest per account) override the automated match.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from pipeline.book_match.config import BookMatchConfig
from pipeline.book_match.matcher import HIGH, REVIEW, UNMATCHED, VERIFIED

log = logging.getLogger("corridoriq.book_match")

ACTIVE, DORMANT, FORMER, UNKNOWN = "ACTIVE", "DORMANT", "FORMER", "UNKNOWN"
IDENTITY_REVIEW = "IDENTITY_REVIEW"
UNMATCHED_ACCOUNT = "UNMATCHED_ACCOUNT"
UNKNOWN_PURCHASE_STATUS = "UNKNOWN_PURCHASE_STATUS"


def purchase_status(last_purchase_on: str | None, cfg: BookMatchConfig, as_of: date) -> str:
    if not last_purchase_on:
        return UNKNOWN
    age = (as_of - date.fromisoformat(last_purchase_on)).days
    if age < 0:
        return UNKNOWN
    if age <= cfg.active_purchase_days:
        return ACTIVE
    if cfg.former_after_days is not None and age > cfg.former_after_days:
        return FORMER
    return DORMANT


def market_status(recent: int, cfg: BookMatchConfig) -> str:
    return "ACTIVE_MARKET" if recent >= cfg.market_min_permits else "QUIET_MARKET"


def market_trend(recent: int, prior: int) -> str:
    if recent == 0 and prior == 0:
        return "NONE"
    if recent > prior:
        return "RISING"
    if recent < prior:
        return "DECLINING"
    return "STEADY"


def classification_for(purchase: str, market: str) -> str:
    if purchase == UNKNOWN:
        return UNKNOWN_PURCHASE_STATUS
    return f"{purchase}_CUSTOMER_{market}"


def _latest_decisions(conn, tid) -> dict:
    out = {}
    for r in conn.execute(
            "SELECT supplier_account_id, decision, company_id FROM review_decisions "
            "WHERE tenant_id=? ORDER BY id", (tid,)):
        out[r["supplier_account_id"]] = (r["decision"], r["company_id"])
    return out


def classify_run(store, run_id: str, index, cfg: BookMatchConfig) -> dict:
    """Write account_classifications and net_new_candidates for one run."""
    cfg.validate()
    as_of = cfg.as_of_date()
    tid = store.tenant_id
    lanes = set(cfg.relevant_lanes)
    counts: dict[str, int] = {}
    confident: set[int] = set()
    review_candidates: dict[int, str] = {}
    rows = []
    with store.connect() as conn:
        run = conn.execute("SELECT run_id FROM match_runs WHERE run_id=? AND tenant_id=?", (run_id, tid)).fetchone()
        if run is None:
            raise LookupError("match run not found for this tenant")
        decisions = _latest_decisions(conn, tid)
        matches = conn.execute(
            "SELECT m.supplier_account_id, m.confidence, m.company_id, m.candidates_json, a.last_purchase_on "
            "FROM account_matches m JOIN book_accounts a USING (supplier_account_id) "
            "WHERE m.run_id=? AND m.tenant_id=? AND a.tenant_id=?", (run_id, tid, tid)).fetchall()
    for m in matches:
        acct = m["supplier_account_id"]
        decision = decisions.get(acct)
        company_id, basis = None, m["confidence"]
        if decision:
            kind, dec_company = decision
            if kind == "REJECT":
                basis, company_id = "HUMAN_REJECTED", None
            else:
                basis, company_id = "HUMAN_CONFIRMED", dec_company
        elif m["confidence"] in (VERIFIED, HIGH):
            company_id = m["company_id"]
        facts = index.get(company_id) if company_id is not None else None
        if company_id is not None and facts is None:
            basis, company_id = "COMPANY_NO_LONGER_ACTIVE", None
        if facts is not None:
            confident.add(company_id)
            p = purchase_status(m["last_purchase_on"], cfg, as_of)
            mk = market_status(facts.recent_permits, cfg)
            cls = classification_for(p, mk)
            rows.append((acct, company_id, basis, cls, p, mk, market_trend(facts.recent_permits, facts.prior_permits),
                         facts.recent_permits, facts.prior_permits, facts.last_permit_date,
                         1 if facts.lanes & lanes else 0))
        else:
            p = purchase_status(m["last_purchase_on"], cfg, as_of)
            cls = IDENTITY_REVIEW if (m["confidence"] == REVIEW and basis == REVIEW) else UNMATCHED_ACCOUNT
            if cls == IDENTITY_REVIEW:
                for cand in json.loads(m["candidates_json"] or "[]"):
                    review_candidates.setdefault(cand["company_id"], acct)
            rows.append((acct, None, basis, cls, p, "NOT_ASSESSED", "NONE", None, None, None, 0))
        counts[rows[-1][3]] = counts.get(rows[-1][3], 0) + 1

    net_new = []
    for cid, f in index.companies.items():
        if cid in confident or not (f.lanes & lanes) or f.recent_permits < cfg.market_min_permits:
            continue
        net_new.append((cid, f))
    net_new.sort(key=lambda x: (-x[1].recent_permits, x[1].display_name or "", x[0]))
    net_new = net_new[: cfg.net_new_limit]

    with store.transaction() as conn:
        conn.execute("DELETE FROM account_classifications WHERE run_id=? AND tenant_id=?", (run_id, tid))
        conn.execute("DELETE FROM net_new_candidates WHERE run_id=? AND tenant_id=?", (run_id, tid))
        conn.executemany(
            "INSERT INTO account_classifications (run_id, tenant_id, supplier_account_id, company_id, match_basis, "
            "classification, purchase_status, market_status, market_trend, recent_permits, prior_permits, "
            "last_permit_date, wet_relevant) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, tid, *r) for r in rows])
        conn.executemany(
            "INSERT INTO net_new_candidates (run_id, tenant_id, company_id, display_name, city, lanes, "
            "recent_permits, last_permit_date, possible_account) VALUES (?,?,?,?,?,?,?,?,?)",
            [(run_id, tid, cid, f.display_name, f.city, ",".join(sorted(f.lanes & lanes)), f.recent_permits,
              f.last_permit_date, review_candidates.get(cid)) for cid, f in net_new])
    counts["NET_NEW_RELEVANT_CONTRACTOR"] = len(net_new)
    log.info("book_match classify tenant=%s run=%s classes=%s", tid, run_id, json.dumps(counts, sort_keys=True))
    return counts
