"""Internal ROC identity-validation report. Not a customer surface."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import (
    CUSTOMER_RELEVANCE_PROFILE,
    REPORTS_GENERATED_DIR,
    ROC_MATCH_VERSION,
    ROC_MODEL_VERSION,
    ROC_SOURCE_PAGE,
)
from pipeline.relevance.account_report import KNOWN, top_accounts
from pipeline.roc.match import HIGH, NO_MATCH, POSSIBLE, VERIFIED
from pipeline.roc.normalize import looks_like_person_name, usable_address, usable_email, usable_phone
from pipeline.roc.simulate import simulate_account_priority
from pipeline.roc.validate import classify_person_account

WATCH = [
    "Arizona Propane",
    "Canyon State Propane",
    "Millennium Gas",
    "Parker & Sons",
    "Gas Piping Inc",
    "Kerns Plumbing",
    "JMax Mechanical",
    "Brincor",
    "Advanced Plumbing",
    "Saguaro Gas",
    "Creative Environments",
    "United Integrated",
    "Austin Commercial",
    "Marketech",
    "Okland",
    "RCI Systems",
    "R C I SYSTEMS",
    "Metro Fire",
]


def coverage_stats(conn: sqlite3.Connection, company_ids: list[int] | None = None) -> dict:
    where = ""
    params: list = [ROC_MATCH_VERSION]
    if company_ids is not None:
        if not company_ids:
            return {
                "n": 0,
                "matched": 0,
                "phone_pct": 0.0,
                "address_pct": 0.0,
                "email_pct": 0.0,
                "status_pct": 0.0,
                "class_pct": 0.0,
                "multi_class_pct": 0.0,
            }
        placeholders = ",".join("?" * len(company_ids))
        where = f" AND m.company_id IN ({placeholders})"
        params.extend(company_ids)
    rows = conn.execute(
        f"""
        SELECT m.company_id, m.match_status, l.phone, l.email, l.address_line_1,
               l.city, l.normalized_status, l.normalized_class, l.corridor_capability
        FROM roc_company_matches m
        LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
        WHERE m.matching_version=? {where}
        """,
        params,
    ).fetchall()
    by = defaultdict(list)
    for row in rows:
        by[int(row["company_id"])].append(dict(row))
    universe = list(company_ids) if company_ids is not None else list(by.keys())
    n = len(universe)
    matched = 0
    phone = address = email = status = klass = multi = 0
    for cid, hits in by.items():
        if hits[0]["match_status"] not in {VERIFIED, HIGH}:
            continue
        matched += 1
        if any(usable_phone(h.get("phone")) for h in hits):
            phone += 1
        if any(usable_address(h) for h in hits):
            address += 1
        if any(usable_email(h.get("email")) for h in hits):
            email += 1
        if any(h.get("normalized_status") == "active" for h in hits):
            status += 1
        if any(h.get("normalized_class") for h in hits):
            klass += 1
        classes = {h.get("normalized_class") for h in hits if h.get("normalized_class")}
        if len(classes) > 1:
            multi += 1
    def pct(x):
        return round(100.0 * x / matched, 1) if matched else 0.0
    return {
        "n": n,
        "matched": matched,
        "phone_pct": pct(phone),
        "address_pct": pct(address),
        "email_pct": pct(email),
        "status_pct": pct(status),
        "class_pct": pct(klass),
        "multi_class_pct": pct(multi),
    }


def account_validation_rows(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    top = top_accounts(conn, limit=limit)
    out = []
    for row in top:
        out.append(_decorate(conn, row))
    return out


def _decorate(conn: sqlite3.Connection, row: dict) -> dict:
    cid = int(row["company_id"])
    matches = [dict(r) for r in conn.execute(
        """
        SELECT m.match_status, m.match_confidence, m.match_reasons,
               l.raw_license_number, l.raw_business_name, l.raw_dba, l.raw_class,
               l.raw_class_detail, l.normalized_status, l.corridor_capability,
               l.qualifying_party, l.address_line_1, l.city, l.postal_code,
               l.phone, l.email, l.normalized_business_name
        FROM roc_company_matches m
        LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
        WHERE m.company_id=? AND m.matching_version=?
        """,
        (cid, ROC_MATCH_VERSION),
    )]
    validation = conn.execute(
        """
        SELECT * FROM roc_identity_validations
        WHERE company_id=? ORDER BY created_at DESC LIMIT 1
        """,
        (cid,),
    ).fetchone()
    d = dict(row)
    d["roc_matches"] = matches
    d["match_status"] = matches[0]["match_status"] if matches else NO_MATCH
    d["roc_classes"] = sorted({m["raw_class"] for m in matches if m.get("raw_class")})
    d["roc_capabilities"] = sorted({m["corridor_capability"] for m in matches if m.get("corridor_capability")})
    d["roc_status"] = sorted({m["normalized_status"] for m in matches if m.get("normalized_status")})
    d["roc_contact"] = {
        "phone": any(usable_phone(m.get("phone")) for m in matches),
        "address": any(usable_address(m) for m in matches),
        "email": any(usable_email(m.get("email")) for m in matches),
    }
    d["person_treatment"] = classify_person_account(row.get("display_name") or "", matches)
    if validation:
        d["validation_result"] = validation["validation_result"]
        d["validation_notes"] = validation["notes"]
        d["confidence_delta"] = validation["confidence_delta"]
    else:
        d["validation_result"] = "INSUFFICIENT_EVIDENCE"
        d["validation_notes"] = "no_match_row"
        d["confidence_delta"] = 0
    return d


def known_validation(conn: sqlite3.Connection) -> list[dict]:
    found = []
    needles = list(dict.fromkeys(KNOWN + WATCH))
    for needle in needles:
        matches = conn.execute(
            """
            SELECT a.*, c.display_name
            FROM company_customer_priority a
            JOIN companies c ON c.id = a.company_id
            WHERE a.profile_key=? AND (
                c.display_name LIKE ? OR c.normalized_name LIKE ?
            )
            ORDER BY a.account_priority_score DESC
            LIMIT 3
            """,
            (CUSTOMER_RELEVANCE_PROFILE, f"%{needle}%", f"%{needle.upper()}%"),
        ).fetchall()
        ranked = {r["company_id"]: r["rank"] for r in top_accounts(conn, limit=50000)}
        items = []
        for row in matches:
            d = dict(row)
            d["rank"] = ranked.get(d["company_id"])
            items.append(_decorate(conn, d))
        found.append({"needle": needle, "matches": items})
    return found


def write_roc_report(
    conn: sqlite3.Connection,
    *,
    ingest_stats: dict,
    match_stats: dict,
    validate_stats: dict,
    simulation: dict | None = None,
    elapsed_s: float | None = None,
) -> Path:
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = REPORTS_GENERATED_DIR / f"roc_identity_validation_{stamp}.md"
    top = account_validation_rows(conn, 100)
    known = known_validation(conn)
    simulation = simulation or simulate_account_priority(conn)
    top_ids = [r["company_id"] for r in top]
    top25_ids = top_ids[:25]
    all_matched_ids = [
        r["company_id"]
        for r in conn.execute(
            "SELECT DISTINCT company_id FROM roc_company_matches WHERE matching_version=? AND match_status IN (?,?)",
            (ROC_MATCH_VERSION, VERIFIED, HIGH),
        )
    ]
    cov_top25 = coverage_stats(conn, top25_ids)
    cov_top100 = coverage_stats(conn, top_ids)
    cov_all = coverage_stats(conn, all_matched_ids)
    person = [r for r in top if looks_like_person_name(r.get("display_name"))]
    fire = [
        r for r in top
        if "fire_protection" in (r.get("roc_capabilities") or [])
        or r.get("primary_demand_category") == "fire_backflow"
    ]
    lines = [
        "# Arizona ROC identity enrichment (internal)",
        "",
        "Shadow validation only. Dashboard ranking, opportunity_score,",
        "customer_relevance_score, and account_priority_score were not changed.",
        "",
        f"- model: `{ROC_MODEL_VERSION}` match: `{ROC_MATCH_VERSION}`",
        f"- source: {ROC_SOURCE_PAGE}",
        f"- ingest: {ingest_stats}",
        f"- match: {match_stats}",
        f"- validate: {validate_stats}",
        f"- elapsed_s: {elapsed_s}",
        "",
        "## Contact coverage (verified/high matches)",
        "",
        f"- Top 25: {cov_top25}",
        f"- Top 100: {cov_top100}",
        f"- All matched companies: {cov_all}",
        "",
        "## Top 100 plumbing_supply accounts vs ROC",
        "",
    ]
    for r in top:
        lines.append(
            f"{r['rank']}. {r['display_name']} | pri={r['account_priority_score']} | "
            f"{r['trade_identity']} | match={r['match_status']} | "
            f"class={','.join(r['roc_classes']) or '—'} | "
            f"status={','.join(r['roc_status']) or '—'} | "
            f"val={r['validation_result']} | Δconf={r['confidence_delta']} | "
            f"contact phone={r['roc_contact']['phone']} addr={r['roc_contact']['address']}"
        )
        lines.append(f"    why: {r.get('validation_notes')} | person={r['person_treatment']}")
    lines += ["", "## Known / watched companies", ""]
    for block in known:
        lines.append(f"### {block['needle']}")
        if not block["matches"]:
            lines.append("- no account row")
            continue
        for m in block["matches"]:
            lines.append(
                f"- {m['display_name']} rank={m.get('rank')} pri={m['account_priority_score']} "
                f"id={m['trade_identity']} match={m['match_status']} "
                f"roc={','.join(m['roc_classes']) or '—'} val={m['validation_result']} "
                f"notes={m.get('validation_notes')}"
            )
    lines += ["", "## Person-name accounts in Top 100", ""]
    if not person:
        lines.append("- none")
    for r in person:
        lines.append(
            f"- rank {r['rank']} {r['display_name']} → {r['person_treatment']} "
            f"match={r['match_status']} roc={','.join(r['roc_classes']) or '—'}"
        )
    lines += ["", "## Fire / backflow in Top 100", ""]
    if not fire:
        lines.append("- none")
    for r in fire:
        lines.append(
            f"- rank {r['rank']} {r['display_name']} demand={r.get('primary_demand_category')} "
            f"roc_caps={r.get('roc_capabilities')} classes={r.get('roc_classes')}"
        )
    dupes = conn.execute(
        """
        SELECT d.*, a.display_name AS name_a, b.display_name AS name_b
        FROM roc_duplicate_candidates d
        JOIN companies a ON a.id = d.company_id_a
        JOIN companies b ON b.id = d.company_id_b
        ORDER BY d.match_confidence DESC
        LIMIT 80
        """
    ).fetchall()
    lines += ["", "## Possible duplicate companies (not merged)", ""]
    if not dupes:
        lines.append("- none")
    for d in dupes:
        lines.append(
            f"- {d['name_a']} ({d['company_id_a']}) ↔ {d['name_b']} ({d['company_id_b']}) "
            f"license={d['normalized_license_number']} reason={d['reason']}"
        )
    lines += ["", "## Simulated ranking impact (not persisted)", ""]
    lines.append(f"- identity flips in simulated Top 25: {len(simulation.get('top25_identity_flips') or [])}")
    for r in (simulation.get("moved") or [])[:25]:
        lines.append(
            f"- {r['display_name']} {r['current_rank']}→{r['simulated_rank']} "
            f"({r['account_priority_score']}→{r['simulated_score']}) "
            f"{r['trade_identity']}→{r['simulated_identity']} {r['simulation_reason']}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path
