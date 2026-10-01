"""Coverage and sales-readiness summaries. Internal only."""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from pipeline.contactability.roles import UNKNOWN
from pipeline.relevance.account_report import top_accounts
from pipeline.roc.match import CONFLICT, POSSIBLE, VERIFIED

HIGH_MIN = 70.0
MEDIUM_MIN = 40.0
ACTIONABLE_TYPES = {
    "business_phone",
    "business_email",
    "website",
    "contact_form",
    "estimator",
    "purchasing",
    "office",
    "sales_contact",
}
NAMED_TYPES = {"named_contact", "owner", "manager", "estimator", "purchasing"}
USEFUL_ROLES = {
    "OWNER_PRINCIPAL",
    "PURCHASING_PROCUREMENT",
    "ESTIMATING",
    "PROJECT_MANAGEMENT",
    "OPERATIONS",
}


def _channels(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT * FROM company_contact_channels
            WHERE company_id=? AND status='active'
            """,
            (company_id,),
        )
    ]


def _usable(ch: dict) -> bool:
    return ch.get("verification_status") in {"VERIFIED", "CANDIDATE"} and int(
        ch.get("public_business_contact") or 0
    )


def account_contact_summary(conn: sqlite3.Connection, company_id: int) -> dict:
    chans = _channels(conn, company_id)
    usable = [c for c in chans if _usable(c)]
    types = {c["contact_type"] for c in usable}
    phones = [c for c in usable if c["contact_type"] == "business_phone" or (
        c["contact_type"] == "office" and c["normalized_value"] and c["normalized_value"].isdigit()
    )]
    emails = [
        c
        for c in usable
        if c["contact_type"] in {"business_email", "estimator", "purchasing", "office"}
        and "@" in (c.get("contact_value") or "")
    ]
    websites = [c for c in usable if c["contact_type"] == "website"]
    forms = [c for c in usable if c["contact_type"] == "contact_form"]
    named = [c for c in usable if c["contact_type"] in NAMED_TYPES and c.get("contact_name")]
    roles = [
        c
        for c in usable
        if c.get("decision_maker_class") in USEFUL_ROLES
    ]
    owners = [c for c in usable if c.get("decision_maker_class") == "OWNER_PRINCIPAL"]
    purch_ops = [
        c
        for c in usable
        if c.get("decision_maker_class") in {
            "PURCHASING_PROCUREMENT",
            "ESTIMATING",
            "OPERATIONS",
            "PROJECT_MANAGEMENT",
        }
    ]
    primary_phone = next((c for c in phones if c.get("is_primary")), phones[0] if phones else None)
    primary_email = next((c for c in emails if c.get("is_primary")), emails[0] if emails else None)
    primary_web = next((c for c in websites if c.get("is_primary")), websites[0] if websites else None)
    named0 = named[0] if named else None
    actionable = bool(types & ACTIONABLE_TYPES)
    best_status = "none"
    if any(c["verification_status"] == "VERIFIED" and c["contact_type"] in ACTIONABLE_TYPES for c in usable):
        best_status = "VERIFIED"
    elif actionable:
        best_status = "CANDIDATE"
    inferred_only = (
        not actionable
        and any(c["verification_status"] == "INFERRED_UNVERIFIED" for c in chans)
    )
    return {
        "has_phone": bool(phones),
        "has_email": bool(emails),
        "has_website": bool(websites),
        "has_form": bool(forms),
        "has_named": bool(named),
        "has_useful_role": bool(roles),
        "has_owner": bool(owners),
        "has_purchasing_ops": bool(purch_ops),
        "actionable": actionable,
        "inferred_only": inferred_only,
        "phone": None if primary_phone is None else primary_phone["contact_value"],
        "email": None if primary_email is None else primary_email["contact_value"],
        "website": None if primary_web is None else primary_web["contact_value"],
        "contact_name": None if named0 is None else named0.get("contact_name"),
        "role": None if named0 is None else named0.get("original_title") or named0.get("title"),
        "decision_class": None if named0 is None else named0.get("decision_maker_class"),
        "contact_confidence": best_status,
        "channel_count": len(usable),
    }


def coverage_for(conn: sqlite3.Connection, accounts: list[dict]) -> dict:
    n = len(accounts) or 1
    tallies = defaultdict(int)
    rows = []
    for acct in accounts:
        cid = int(acct["company_id"])
        summary = account_contact_summary(conn, cid)
        match = conn.execute(
            "SELECT match_status FROM roc_company_matches WHERE company_id=? LIMIT 1",
            (cid,),
        ).fetchone()
        match_status = None if match is None else match["match_status"]
        override = conn.execute(
            "SELECT recommended_match_status FROM entity_match_overrides WHERE company_id=? LIMIT 1",
            (cid,),
        ).fetchone()
        recommended = None if override is None else override["recommended_match_status"]
        pri = float(acct.get("account_priority_score") or 0)
        band = "HIGH" if pri >= HIGH_MIN else "MEDIUM" if pri >= MEDIUM_MIN else "LOW"
        identity_verified = match_status == VERIFIED
        identity_ambiguous = match_status in {POSSIBLE, CONFLICT}
        summary.update(
            {
                "rank": acct.get("rank"),
                "company_id": cid,
                "display_name": acct.get("display_name"),
                "account_priority_score": pri,
                "priority_band": band,
                "trade_identity": acct.get("trade_identity"),
                "primary_demand_category": acct.get("primary_demand_category"),
                "why_now": acct.get("why_now"),
                "relevant_90d": acct.get("relevant_90d"),
                "match_status": match_status,
                "recommended_match_status": recommended,
                "identity_verified": identity_verified,
                "identity_ambiguous": identity_ambiguous,
            }
        )
        for key in (
            "has_phone",
            "has_email",
            "has_website",
            "has_form",
            "has_named",
            "has_useful_role",
            "has_owner",
            "has_purchasing_ops",
            "actionable",
            "identity_verified",
            "identity_ambiguous",
        ):
            if summary[key]:
                tallies[key] += 1
        if summary["actionable"] and band == "HIGH":
            tallies["contactable_high"] += 1
        if summary["actionable"] and band == "MEDIUM":
            tallies["contactable_medium"] += 1
        if band == "HIGH" and not summary["actionable"]:
            tallies["high_no_contact"] += 1
            summary["bucket"] = "HIGH_PRIORITY_NO_CONTACT"
        elif identity_ambiguous:
            tallies["identity_ambiguous_bucket"] += 1
            summary["bucket"] = "IDENTITY_AMBIGUOUS"
        elif summary["actionable"] and band == "HIGH":
            summary["bucket"] = "CONTACTABLE_HIGH"
        elif summary["actionable"] and band == "MEDIUM":
            summary["bucket"] = "CONTACTABLE_MEDIUM"
        else:
            summary["bucket"] = "OTHER"
        summary["next_action"] = _next_action(summary)
        rows.append(summary)
    pct = {k: round(100.0 * v / n, 1) for k, v in tallies.items()}
    return {"n": len(accounts), "counts": dict(tallies), "pct": pct, "rows": rows}


def _next_action(row: dict) -> str:
    if row["identity_ambiguous"] and row["actionable"]:
        return "Confirm legal entity before first call; use public business line."
    if row["identity_ambiguous"] and not row["actionable"]:
        return "Resolve identity (ROC license uniqueness) then find a public business contact."
    if row.get("priority_band") == "HIGH" and not row["actionable"]:
        return "Public-website contact pass — high-value account with no channel."
    if row["has_phone"] and not row["has_named"]:
        return "Call main business line; ask for purchasing or the estimator."
    if row["has_email"] and row.get("decision_class") in USEFUL_ROLES:
        return "Email the named role with a supply-sheet offer."
    if row["actionable"]:
        return "Initiate contact via the primary public channel."
    return "No actionable channel yet; skip personal-number hunting."


def decision_maker_coverage(conn: sqlite3.Connection, accounts: list[dict]) -> dict:
    counts = defaultdict(int)
    for acct in accounts:
        named = [
            c
            for c in _channels(conn, int(acct["company_id"]))
            if _usable(c)
            and c.get("contact_type") != "qualifying_party"
            and (c.get("contact_name") or c.get("original_title"))
        ]
        if not named:
            counts[UNKNOWN] += 1
        else:
            classes = {c.get("decision_maker_class") or UNKNOWN for c in named}
            for cl in classes:
                counts[cl] += 1
    return dict(counts)
