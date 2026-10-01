"""Permit-behavior identity vs ROC license identity. Does not change ranking."""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict

from pipeline.config.settings import (
    CUSTOMER_RELEVANCE_PROFILE,
    ROC_MATCH_VERSION,
    ROC_MODEL_VERSION,
)
from pipeline.db.database import now_iso
from pipeline.roc.classifications import (
    CAP_FIRE,
    CAP_FUEL_GAS,
    CAP_GC,
    CAP_HVAC,
    CAP_PLUMBING,
    CAP_SITE,
    capabilities_for_licenses,
)
from pipeline.roc.match import CONFLICT, HIGH, NO_MATCH, POSSIBLE, VERIFIED
from pipeline.roc.normalize import looks_like_person_name, usable_address, usable_email, usable_phone

CONFIRMED = "CONFIRMED"
PARTIAL = "PARTIALLY_CONFIRMED"
CONTRADICTED = "CONTRADICTED"
ROC_ADDS = "ROC_ADDS_CAPABILITY"
PERMIT_ADDS = "PERMIT_BEHAVIOR_ADDS_ACTIVITY"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

_TRADE_CAPS = {CAP_PLUMBING, CAP_FUEL_GAS, CAP_HVAC, CAP_SITE}
_POSITIVE = {VERIFIED, HIGH}


def validate_identities(conn: sqlite3.Connection, *, profile_key: str | None = None) -> dict:
    profile_key = profile_key or CUSTOMER_RELEVANCE_PROFILE
    now = now_iso()
    conn.execute(
        "DELETE FROM roc_identity_validations WHERE model_version=?",
        (ROC_MODEL_VERSION,),
    )
    conn.execute("DELETE FROM roc_contact_candidates")
    conn.execute("DELETE FROM roc_duplicate_candidates")

    matches_by: dict[int, list[dict]] = defaultdict(list)
    for row in conn.execute(
        """
        SELECT m.*, l.corridor_capability, l.secondary_capabilities, l.normalized_status,
               l.raw_class, l.raw_class_detail, l.raw_business_name, l.raw_dba,
               l.qualifying_party, l.address_line_1, l.city, l.state, l.postal_code,
               l.phone, l.email, l.issued_date, l.expiration_date,
               l.normalized_license_number, l.normalized_business_name
        FROM roc_company_matches m
        LEFT JOIN roc_licenses l ON l.id = m.roc_license_id
        WHERE m.matching_version = ?
        """,
        (ROC_MATCH_VERSION,),
    ):
        matches_by[int(row["company_id"])].append(dict(row))

    companies = {
        int(r["id"]): dict(r)
        for r in conn.execute(
            """
            SELECT id, display_name, main_phone, main_email, address_line_1, city,
                   postal_code
            FROM companies
            """
        )
    }
    accounts = {
        int(r["company_id"]): dict(r)
        for r in conn.execute(
            """
            SELECT * FROM company_customer_priority WHERE profile_key=?
            """,
            (profile_key,),
        )
    }

    val_rows = []
    contact_rows = []
    hist = defaultdict(int)
    for company_id, hits in matches_by.items():
        company = companies.get(company_id) or {}
        account = accounts.get(company_id)
        permit_id = None if account is None else account.get("trade_identity")
        mapped = []
        for hit in hits:
            if not hit.get("corridor_capability"):
                continue
            secondary = []
            try:
                secondary = json.loads(hit.get("secondary_capabilities") or "[]")
            except json.JSONDecodeError:
                secondary = []
            mapped.append(
                {
                    "corridor_capability": hit["corridor_capability"],
                    "secondary_capabilities": secondary,
                    "status": hit.get("normalized_status"),
                    "raw_class": hit.get("raw_class"),
                }
            )
        caps = capabilities_for_licenses(mapped)
        statuses = {h.get("normalized_status") for h in hits if h.get("normalized_status")}
        match_status = hits[0]["match_status"] if hits else NO_MATCH
        result, delta, notes = _compare(permit_id, caps, match_status, statuses)
        hist[result] += 1
        val_rows.append(
            (
                company_id,
                profile_key,
                permit_id,
                json.dumps(caps),
                ",".join(sorted(s for s in statuses if s)),
                result,
                delta,
                notes,
                ROC_MODEL_VERSION,
                now,
            )
        )
        contact_rows.extend(_contact_candidates(company_id, company, hits, now))

    conn.executemany(
        """
        INSERT INTO roc_identity_validations (
            company_id, profile_key, permit_identity, roc_capabilities,
            roc_license_status, validation_result, confidence_delta, notes,
            model_version, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?)
        """,
        val_rows,
    )
    if contact_rows:
        conn.executemany(
            """
            INSERT OR IGNORE INTO roc_contact_candidates (
                company_id, field_name, value, source_family, source_date,
                confidence, verification_status, would_overwrite_existing, created_at
            ) VALUES (?,?,?,'roc',?,?,?,?,?)
            """,
            contact_rows,
        )
    dupes = _duplicate_rows(conn, matches_by, companies, now)
    if dupes:
        conn.executemany(
            """
            INSERT OR IGNORE INTO roc_duplicate_candidates (
                company_id_a, company_id_b, normalized_license_number,
                roc_business_name, reason, match_confidence, created_at
            ) VALUES (?,?,?,?,?,?,?)
            """,
            dupes,
        )
    conn.commit()
    return {
        "validations": len(val_rows),
        "results": dict(hist),
        "contact_candidates": len(contact_rows),
        "duplicate_groups": len(dupes),
        "model_version": ROC_MODEL_VERSION,
    }


def classify_person_account(display_name: str, hits: list[dict]) -> str:
    if not looks_like_person_name(display_name):
        return "not_person_name"
    if not hits or hits[0].get("match_status") in {NO_MATCH, None}:
        return "unmatched_individual"
    reasons = []
    for hit in hits:
        try:
            reasons.extend(json.loads(hit.get("match_reasons") or "[]"))
        except json.JSONDecodeError:
            pass
        name = (hit.get("normalized_business_name") or "").upper()
        if looks_like_person_name(hit.get("raw_business_name")):
            return "licensed_sole_proprietor"
        if "qualifying_party" in reasons:
            return "qualifying_party"
        if hit.get("raw_dba"):
            return "company_alias"
    if any(h.get("match_status") in _POSITIVE for h in hits):
        return "licensed_business"
    return "ambiguous_identity"


def _compare(permit_id, caps, match_status, statuses) -> tuple[str, float, str]:
    if match_status not in _POSITIVE or not caps:
        if match_status == CONFLICT:
            return CONTRADICTED, -8.0, "conflicting_entity_match"
        if match_status == POSSIBLE:
            return INSUFFICIENT, 2.0, "possible_match_only"
        return INSUFFICIENT, 0.0, "no_authoritative_match"
    active = "active" in statuses or not statuses
    roc_trade = bool(set(caps) & _TRADE_CAPS)
    roc_gc = CAP_GC in caps
    roc_fire = CAP_FIRE in caps
    roc_plumb = CAP_PLUMBING in caps or CAP_FUEL_GAS in caps

    if permit_id in {"plumbing_specialist", "recurring_plumbing"} and roc_plumb:
        return CONFIRMED, 12.0, "plumbing_license_confirms_behavior"
    if permit_id in {"fuel_gas_specialist", "recurring_fuel_gas"} and roc_plumb:
        return CONFIRMED, 12.0, "plumbing_or_gas_license_confirms_fuel_gas"
    if permit_id in {"mechanical_wet"} and (CAP_HVAC in caps or roc_plumb):
        return CONFIRMED, 10.0, "mechanical_license_confirms_behavior"
    if permit_id in {"site_utility"} and CAP_SITE in caps:
        return CONFIRMED, 10.0, "site_utility_license_confirms_behavior"
    if permit_id == "gc_with_plumbing_demand" and roc_gc:
        if roc_plumb:
            return PARTIAL, 6.0, "gc_license_plus_trade_license"
        return CONFIRMED, 8.0, "gc_license_confirms_gc_behavior"
    if permit_id in {"plumbing_specialist", "recurring_plumbing"} and roc_gc and not roc_plumb:
        return CONTRADICTED, -10.0, "behavior_plumber_roc_gc_only"
    if permit_id in {"gc_with_plumbing_demand"} and roc_plumb and not roc_gc:
        return ROC_ADDS, 8.0, "roc_trade_license_on_gc_behavior"
    if permit_id in {"unknown", "incidental_trade", "other_trade"} and roc_plumb:
        return ROC_ADDS, 10.0, "roc_adds_trade_capability"
    if permit_id in {"plumbing_specialist", "fuel_gas_specialist"} and roc_fire and not roc_plumb:
        return PARTIAL, 4.0, "fire_protection_adjacent_not_fixture_plumbing"
    if roc_gc and permit_id in {"plumbing_specialist", "fuel_gas_specialist"}:
        return PERMIT_ADDS, 0.0, "permits_show_trade_activity_beyond_gc_license"
    if roc_trade and permit_id == "gc_with_plumbing_demand":
        return PERMIT_ADDS, 4.0, "gc_behavior_with_trade_license_activity"
    if active and roc_trade:
        return PARTIAL, 4.0, "some_trade_overlap"
    return INSUFFICIENT, 0.0, "matched_without_decisive_overlap"


def _contact_candidates(company_id, company, hits, now) -> list[tuple]:
    out = []
    fields = {
        "main_phone": "phone",
        "main_email": "email",
        "address_line_1": "address_line_1",
        "city": "city",
        "postal_code": "postal_code",
        "dba_name": "raw_dba",
        "legal_name": "raw_business_name",
        "qualifying_party": "qualifying_party",
        "license_number": "normalized_license_number",
    }
    for dest, src in fields.items():
        values = []
        for hit in hits:
            val = (hit.get(src) or "").strip()
            if src == "phone":
                val = usable_phone(val) or ""
            if src == "email":
                val = usable_email(val) or ""
            if val:
                values.append(val)
        if not values:
            continue
        value = values[0]
        existing = (company.get(dest) if dest in company else None) or ""
        if dest == "license_number":
            existing = ""
        overwrite = bool(str(existing).strip()) and str(existing).strip().upper() != value.upper()
        out.append(
            (
                company_id,
                dest,
                value,
                hit_date(hits),
                90.0 if hits[0]["match_status"] in _POSITIVE else 60.0,
                "candidate",
                1 if overwrite else 0,
                now,
            )
        )
    return out


def hit_date(hits: list[dict]) -> str | None:
    for hit in hits:
        if hit.get("issued_date"):
            return hit["issued_date"]
    return None


def _duplicate_rows(conn, matches_by, companies, now) -> list[tuple]:
    by_lic: dict[str, set[int]] = defaultdict(set)
    by_name: dict[str, set[int]] = defaultdict(set)
    names = {}
    for cid, hits in matches_by.items():
        if not hits or hits[0]["match_status"] not in _POSITIVE:
            continue
        for hit in hits:
            lic = hit.get("normalized_license_number")
            if lic:
                by_lic[lic].add(cid)
                names[(cid, lic)] = hit.get("raw_business_name")
            bname = hit.get("normalized_business_name")
            if bname:
                by_name[bname].add(cid)
    out = []
    seen = set()
    for lic, ids in by_lic.items():
        if len(ids) < 2:
            continue
        ordered = sorted(ids)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                key = (a, b, lic)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    (
                        a,
                        b,
                        lic,
                        names.get((a, lic)),
                        "same_roc_license",
                        95.0,
                        now,
                    )
                )
    return out
