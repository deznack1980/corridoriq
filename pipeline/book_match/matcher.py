"""Match a supplier's accounts to CorridorIQ companies — conservatively.

Principle: UNMATCHED is better than a confident wrong match.

Evidence per candidate company:
  license_match   ROC license on the account equals a license tied to the company
  phone_match     normalized phone equals a company phone (verified or candidate channel)
  address_match   street key equal and ZIP (or city) equal
  name_exact      compacted company name equal (suffix/punctuation/THE insensitive)
  dba_exact       account DBA equals a company name or alias
  name_similar    high token overlap (never decisive alone)
Conflicts: the account and company both have a value of a kind and none agree
(license, verified phone, street address with the same ZIP area, city).

Confidence (deterministic, no numeric score exposed):
  VERIFIED         license_match + any corroboration, no conflicts, unique
  HIGH_CONFIDENCE  exact name/DBA + phone or address, no conflicts, unique
                   (or phone + address + similar name)
  REVIEW_REQUIRED  any evidence that does not meet the above, any conflict,
                   or more than one company qualifying equally
  UNMATCHED        no candidate
Name similarity alone can reach REVIEW_REQUIRED at most. By default, exact name
plus city alone also stays REVIEW_REQUIRED (configurable, off by default).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pipeline.book_match import normalize as N
from pipeline.book_match.config import RULES_VERSION, BookMatchConfig
from pipeline.book_match.schema import BOOK_SCHEMA

log = logging.getLogger("corridoriq.book_match")

VERIFIED = "VERIFIED"
HIGH = "HIGH_CONFIDENCE"
REVIEW = "REVIEW_REQUIRED"
UNMATCHED = "UNMATCHED"
_RANK = {VERIFIED: 3, HIGH: 2, REVIEW: 1}
_IDENTIFYING = {"license_match", "phone_match", "address_match"}

_LABEL = {
    "license_match": "ROC license matches",
    "phone_match": "phone matches",
    "address_match": "street address matches",
    "name_exact": "company name matches exactly",
    "dba_exact": "DBA matches a company name",
    "name_similar": "company name is similar",
    "city_match": "same city",
    "license_conflict": "different ROC license",
    "phone_conflict": "different verified phone",
    "address_conflict": "different street address in the same area",
    "city_conflict": "different city",
}


@dataclass
class Candidate:
    company_id: int
    evidence: set = field(default_factory=set)
    conflicts: set = field(default_factory=set)
    level: str = REVIEW


@dataclass
class MatchResult:
    confidence: str
    company_id: int | None
    evidence: list
    conflicts: list
    candidates: list
    review_reason: str | None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _evaluate(acct: dict, f, ev: set, cfg: BookMatchConfig) -> Candidate:
    c = Candidate(f.company_id, set(ev))
    keys = [k for k in (acct["name_key"], acct["dba_key"]) if k]
    if acct["name_key"] and acct["name_key"] in f.name_keys:
        c.evidence.add("name_exact")
    if acct["dba_key"] and acct["dba_key"] in f.name_keys:
        c.evidence.add("dba_exact")
    if not ({"name_exact", "dba_exact"} & c.evidence) and keys:
        best = max((N.name_similarity(k, fk) for k in keys for fk in f.name_keys), default=0.0)
        if best >= cfg.name_review_min_similarity:
            c.evidence.add("name_similar")
    if acct["license_key"] and f.licenses and acct["license_key"] not in f.licenses:
        c.conflicts.add("license_conflict")
    verified_phones = {p for p, s in f.phones.items() if s == "VERIFIED"}
    if acct["phone_key"] and verified_phones and acct["phone_key"] not in f.phones:
        c.conflicts.add("phone_conflict")
    if acct["street_key"] and "address_match" not in c.evidence:
        same_area = [s for s in f.streets
                     if (acct["zip5"] and s[1] == acct["zip5"]) or (acct["city_key"] and s[2] == acct["city_key"])]
        if same_area:
            c.conflicts.add("address_conflict")
    if acct["city_key"] and f.cities:
        c.evidence.add("city_match") if acct["city_key"] in f.cities else c.conflicts.add("city_conflict")

    name = bool({"name_exact", "dba_exact"} & c.evidence)
    contact = bool({"phone_match", "address_match"} & c.evidence)
    # A different city is informational (company cities can come from job
    # sites); license, verified-phone and same-area address conflicts block.
    hard_conflict = bool(c.conflicts - {"city_conflict"})
    if hard_conflict:
        c.level = REVIEW
    elif "license_match" in c.evidence and (name or contact or "name_similar" in c.evidence):
        c.level = VERIFIED
    elif name and contact:
        c.level = HIGH
    elif {"phone_match", "address_match"} <= c.evidence and "name_similar" in c.evidence:
        c.level = HIGH
    elif (cfg.allow_name_city_high_confidence and name and "city_match" in c.evidence
          and not f.is_person and not acct["is_person_name"]):
        c.level = HIGH
    else:
        c.level = REVIEW
    return c


def _strength(c: Candidate) -> tuple:
    return (_RANK[c.level], len(c.evidence & _IDENTIFYING), len(c.evidence), -len(c.conflicts), -c.company_id)


def match_account(acct: dict, index, cfg: BookMatchConfig) -> MatchResult:
    seeds: dict[int, set] = {}

    def add(ids, label):
        for cid in ids:
            if cid in index.companies:
                seeds.setdefault(cid, set()).add(label)

    if acct["license_key"]:
        add(index.by_license.get(acct["license_key"], ()), "license_match")
    if acct["phone_key"]:
        add(index.by_phone.get(acct["phone_key"], ()), "phone_match")
    if acct["street_key"]:
        for cid in index.by_street.get(acct["street_key"], ()):
            f = index.companies.get(cid)
            if f and any(s[0] == acct["street_key"] and
                         ((acct["zip5"] and s[1] == acct["zip5"]) or (acct["city_key"] and s[2] == acct["city_key"]))
                         for s in f.streets):
                add([cid], "address_match")
    for key in (acct["name_key"], acct["dba_key"]):
        if key:
            add(index.by_name.get(key, ()), "name_seed")
            for tok in N.distinctive(key):
                add(index.by_token.get(tok, ()), "token_seed")

    cands = []
    for cid, ev in seeds.items():
        c = _evaluate(acct, index.companies[cid], ev - {"name_seed", "token_seed"}, cfg)
        if c.evidence - {"city_match"}:
            cands.append(c)
    if not cands:
        return MatchResult(UNMATCHED, None, [], [], [], "no CorridorIQ company shares identifying details")
    cands.sort(key=_strength, reverse=True)
    top = cands[0]
    peers = [c for c in cands[1:] if c.level == top.level]
    others_identifying = [c for c in cands[1:] if c.evidence & _IDENTIFYING]
    reason = None
    level = top.level
    if level in (VERIFIED, HIGH) and peers:
        level, reason = REVIEW, f"{len(peers) + 1} CorridorIQ companies qualify equally"
    elif level in (VERIFIED, HIGH) and others_identifying:
        level, reason = REVIEW, "another CorridorIQ company shares this account's license, phone or address"
    elif level == REVIEW:
        reason = _review_reason(acct, top, cands)
    shown = cands[: cfg.max_candidates]
    return MatchResult(
        confidence=level,
        company_id=top.company_id,
        evidence=sorted(top.evidence),
        conflicts=sorted(top.conflicts),
        candidates=[{"company_id": c.company_id, "level": c.level,
                     "evidence": sorted(c.evidence), "conflicts": sorted(c.conflicts)} for c in shown],
        review_reason=reason,
    )


def _review_reason(acct, top: Candidate, cands) -> str:
    hard = top.conflicts - {"city_conflict"}
    if hard:
        return "conflicting evidence: " + ", ".join(_LABEL[x] for x in sorted(hard))
    same_name = [c for c in cands if {"name_exact", "dba_exact"} & c.evidence]
    if len(same_name) > 1:
        return f"name matches {len(same_name)} CorridorIQ companies; no corroborating license, phone or address"
    ident = top.evidence & _IDENTIFYING
    if ident and not ({"name_exact", "dba_exact", "name_similar"} & top.evidence):
        return "only " + " and ".join(_LABEL[x] for x in sorted(ident)) + "; the names differ"
    if acct["is_person_name"]:
        return "account name looks like a person (sole proprietor); confirm identity"
    if "name_similar" in top.evidence and not ident:
        return "similar name only; no license, phone or address to confirm"
    if {"name_exact", "dba_exact"} & top.evidence and not ident:
        return "name matches but no license, phone or address to confirm"
    return "single identifying detail only; confirm before use"


def explain(labels) -> str:
    return "; ".join(_LABEL.get(x, x) for x in labels)


def match_book(store, index, cfg: BookMatchConfig, batch_id: str) -> tuple[str, dict]:
    """Match every account in the latest snapshot. Writes one match run."""
    cfg.validate()
    started = time.perf_counter()
    store.ensure_schema(BOOK_SCHEMA)
    tid = store.tenant_id
    run_id = uuid.uuid4().hex
    counts = {VERIFIED: 0, HIGH: 0, REVIEW: 0, UNMATCHED: 0}
    with store.connect() as conn:
        accounts = [dict(r) for r in conn.execute(
            "SELECT n.* FROM account_normalized n JOIN book_accounts a USING (supplier_account_id) "
            "WHERE a.in_latest_batch=1 AND a.tenant_id=? AND n.tenant_id=? ORDER BY a.supplier_account_id",
            (tid, tid))]
    results = [(a["supplier_account_id"], match_account(a, index, cfg)) for a in accounts]
    with store.transaction() as conn:
        conn.execute(
            "INSERT INTO match_runs (run_id, tenant_id, batch_id, rules_version, intel_fingerprint, "
            "config_json, started_at) VALUES (?,?,?,?,?,?,?)",
            (run_id, tid, batch_id, RULES_VERSION, index.fingerprint, cfg.to_json(), _now()))
        for acct_id, m in results:
            counts[m.confidence] += 1
            conn.execute(
                "INSERT INTO account_matches (run_id, tenant_id, supplier_account_id, confidence, company_id, "
                "evidence_json, conflicts_json, candidates_json, review_reason) VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, tid, acct_id, m.confidence, m.company_id if m.confidence != UNMATCHED else None,
                 json.dumps(m.evidence), json.dumps(m.conflicts), json.dumps(m.candidates), m.review_reason))
        conn.execute("UPDATE match_runs SET finished_at=?, counts_json=? WHERE run_id=?",
                     (_now(), json.dumps(counts), run_id))
    log.info("book_match match tenant=%s run=%s accounts=%d verified=%d high=%d review=%d unmatched=%d seconds=%.2f",
             tid, run_id, len(results), counts[VERIFIED], counts[HIGH], counts[REVIEW], counts[UNMATCHED],
             time.perf_counter() - started)
    return run_id, counts
