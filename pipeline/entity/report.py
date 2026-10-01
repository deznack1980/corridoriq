"""Phase 4D internal report. Not a customer or dashboard surface."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import (
    CONTACT_MODEL_VERSION,
    ENTITY_MODEL_VERSION,
    REPORTS_GENERATED_DIR,
    ROC_DATA_DIR,
)
from pipeline.contactability.coverage import (
    coverage_for,
    decision_maker_coverage,
)
from pipeline.entity.names import compact_company_name
from pipeline.relevance.account_report import top_accounts

WATCH_NOTES = {
    40676: (
        "Arizona Propane stays POSSIBLE. The name is a DBA used by more than one "
        "ROC licensee. The official website confirms the brand, not a unique license."
    ),
    37857: (
        "Parker & Sons stays POSSIBLE. Multiple ROC legal entities use Parker and Sons "
        "as a DBA (including Environmental Conditioning LLC). Website/phone confirm the "
        "consumer brand, not which licensee pulled the permits."
    ),
    38349: "Kerns Plumbing is already VERIFIED (name + geo). Variant KERNS PLUMBING L L C is a punctuation alias.",
    38307: "Gas Piping Inc is HIGH_CONFIDENCE on exact name. GAS PIPING INC (2) is a source-disambiguation alias of the same commercial account.",
    38855: "Whiting-Turner license 256012 is shared by two CorridorIQ rows; canonical-link only, no merge.",
    47553: "R C I SYSTEMS INC and RCI SYSTEMS, INC. compact to the same key. Rank 3 and rank 5 are one account.",
    45906: "Petra Contracting remains CONFLICT/CONTRADICTED until a unique license+name+ZIP triple exists. Do not fuzzy-upgrade.",
    45895: "REDPOINT CONTRACTING remains CONFLICT. Do not consume in ranking.",
}


def score_guardrails(conn: sqlite3.Connection) -> dict:
    opp = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(opportunity_score),0) s FROM projects"
    ).fetchone()
    rel = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(relevance_score),0) s FROM project_customer_relevance"
    ).fetchone()
    pri = conn.execute(
        "SELECT COUNT(*) n, COALESCE(SUM(account_priority_score),0) s FROM company_customer_priority"
    ).fetchone()
    roc_on = conn.execute(
        "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='roc'"
    ).fetchone()
    contact_on = conn.execute(
        "SELECT is_enabled FROM enrichment_source_registry WHERE source_family='contact'"
    ).fetchone()
    return {
        "projects": int(opp["n"]),
        "opportunity_score_sum": float(opp["s"]),
        "relevance_rows": int(rel["n"]),
        "relevance_score_sum": float(rel["s"]),
        "priority_rows": int(pri["n"]),
        "account_priority_score_sum": float(pri["s"]),
        "roc_enabled": int(roc_on[0] if roc_on else 0),
        "contact_enabled": int(contact_on[0] if contact_on else 0),
        "crm_relationships": int(
            conn.execute("SELECT COUNT(*) n FROM crm_company_relationships").fetchone()["n"]
        ),
        "merged_companies": int(
            conn.execute(
                "SELECT COUNT(*) n FROM companies "
                "WHERE merged_into_id IS NOT NULL OR lifecycle_state='merged'"
            ).fetchone()["n"]
        ),
    }


def duplicate_table(conn: sqlite3.Connection) -> list[dict]:
    rows = []
    for row in conn.execute(
        """
        SELECT r.*, a.display_name AS name_a, a.normalized_name AS norm_a,
               b.display_name AS name_b, b.normalized_name AS norm_b
        FROM entity_duplicate_reviews r
        JOIN companies a ON a.id=r.company_id_a
        JOIN companies b ON b.id=r.company_id_b
        ORDER BY r.classification, r.company_id_a
        """
    ):
        ev = json.loads(row["evidence"] or "{}")
        rows.append(
            {
                "id_a": row["company_id_a"],
                "id_b": row["company_id_b"],
                "name_a": row["name_a"],
                "name_b": row["name_b"],
                "norm_a": row["norm_a"],
                "norm_b": row["norm_b"],
                "compact_a": ev.get("compact_a"),
                "compact_b": ev.get("compact_b"),
                "license": row["normalized_license_number"],
                "roc_legal": ev.get("roc_legal"),
                "roc_dba": ev.get("roc_dba"),
                "address": ev.get("address"),
                "city": ev.get("city"),
                "zip": ev.get("postal_code"),
                "phone_a": (ev.get("snapshot_a") or {}).get("phone"),
                "phone_b": (ev.get("snapshot_b") or {}).get("phone"),
                "email_a": (ev.get("snapshot_a") or {}).get("email"),
                "email_b": (ev.get("snapshot_b") or {}).get("email"),
                "projects_a": (ev.get("snapshot_a") or {}).get("projects"),
                "projects_b": (ev.get("snapshot_b") or {}).get("projects"),
                "permits_a": (ev.get("snapshot_a") or {}).get("permits"),
                "permits_b": (ev.get("snapshot_b") or {}).get("permits"),
                "latest_a": (ev.get("snapshot_a") or {}).get("latest"),
                "latest_b": (ev.get("snapshot_b") or {}).get("latest"),
                "caps_a": (ev.get("snapshot_a") or {}).get("capabilities"),
                "caps_b": (ev.get("snapshot_b") or {}).get("capabilities"),
                "priority_a": (ev.get("snapshot_a") or {}).get("priority"),
                "priority_b": (ev.get("snapshot_b") or {}).get("priority"),
                "match_a": (ev.get("snapshot_a") or {}).get("match"),
                "match_b": (ev.get("snapshot_b") or {}).get("match"),
                "classification": row["classification"],
                "canonical_name": row["canonical_recommendation"],
                "reasons": ev.get("reasons"),
            }
        )
    return rows


def fulfillment_gaps(row: dict) -> list[str]:
    gaps = []
    if row.get("identity_ambiguous"):
        gaps.append("unique_legal_entity")
    if not row.get("identity_verified") and row.get("match_status") not in {"HIGH_CONFIDENCE_MATCH"}:
        gaps.append("verified_license_identity")
    if not row.get("has_phone") and not row.get("has_email"):
        gaps.append("actionable_business_contact")
    if not row.get("has_named"):
        gaps.append("named_decision_maker")
    if not row.get("has_useful_role"):
        gaps.append("purchasing_or_estimator_role")
    if not row.get("primary_demand_category"):
        gaps.append("likely_demand_category")
    gaps.append("material_list_or_supply_sheet")
    gaps.append("jobsite_delivery_window")
    gaps.append("preferred_fulfillment_partner")
    return gaps


def _pct_line(cov: dict) -> list[str]:
    p = cov["pct"]
    n = cov["n"]
    return [
        f"- business phone: {p.get('has_phone', 0)}% ({cov['counts'].get('has_phone', 0)}/{n})",
        f"- business email: {p.get('has_email', 0)}% ({cov['counts'].get('has_email', 0)}/{n})",
        f"- website: {p.get('has_website', 0)}% ({cov['counts'].get('has_website', 0)}/{n})",
        f"- named decision-maker: {p.get('has_named', 0)}% ({cov['counts'].get('has_named', 0)}/{n})",
        f"- useful role/title: {p.get('has_useful_role', 0)}% ({cov['counts'].get('has_useful_role', 0)}/{n})",
        f"- at least one actionable channel: {p.get('actionable', 0)}% ({cov['counts'].get('actionable', 0)}/{n})",
        f"- ROC identity-verified (VERIFIED_MATCH): {p.get('identity_verified', 0)}% ({cov['counts'].get('identity_verified', 0)}/{n})",
        f"- ambiguous entity (POSSIBLE/CONFLICT): {p.get('identity_ambiguous', 0)}% ({cov['counts'].get('identity_ambiguous', 0)}/{n})",
    ]


def write_phase4d_report(
    conn: sqlite3.Connection,
    *,
    entity_stats: dict,
    inventory_stats: dict,
    research_stats: dict,
    official_stats: dict,
    elapsed_s: float,
    before_guard: dict,
    after_guard: dict,
) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    top25 = top_accounts(conn, limit=25)
    top100 = top_accounts(conn, limit=100)
    cov25 = coverage_for(conn, top25)
    cov100 = coverage_for(conn, top100)
    dm25 = decision_maker_coverage(conn, top25)
    dm100 = decision_maker_coverage(conn, top100)
    dupes = duplicate_table(conn)
    class_counts = Counter(r["classification"] for r in dupes)
    overrides = [dict(r) for r in conn.execute("SELECT * FROM entity_match_overrides")]
    high_no = [r for r in cov100["rows"] if r["bucket"] == "HIGH_PRIORITY_NO_CONTACT"]
    canonical_n = conn.execute("SELECT COUNT(*) n FROM canonical_companies").fetchone()["n"]
    link_n = conn.execute("SELECT COUNT(*) n FROM company_entity_links").fetchone()["n"]
    channel_n = conn.execute("SELECT COUNT(*) n FROM company_contact_channels").fetchone()["n"]

    payload = {
        "generated_at": ts,
        "entity_stats": entity_stats,
        "inventory_stats": inventory_stats,
        "research_stats": research_stats,
        "official_stats": official_stats,
        "coverage_top25": {k: cov25[k] for k in ("n", "counts", "pct")},
        "coverage_top100": {k: cov100[k] for k in ("n", "counts", "pct")},
        "top25_rows": cov25["rows"],
        "top100_high_no_contact": high_no,
        "duplicates": dupes,
        "duplicate_class_counts": dict(class_counts),
        "overrides": overrides,
        "decision_makers_top25": dm25,
        "decision_makers_top100": dm100,
        "guard_before": before_guard,
        "guard_after": after_guard,
        "elapsed_s": elapsed_s,
        "canonical_companies": int(canonical_n),
        "entity_links": int(link_n),
        "contact_channels": int(channel_n),
    }
    ROC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    json_path = ROC_DATA_DIR / f"phase4d_entity_contactability_{ts}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Phase 4D — Entity resolution + contactability (INTERNAL)",
        "",
        f"Generated {ts}. Models `{ENTITY_MODEL_VERSION}` / `{CONTACT_MODEL_VERSION}`.",
        "Dashboard, production ranking, CRM, ROC ranking consumption, opportunity_score,",
        "customer_relevance_score, and account_priority_score were not changed.",
        "",
        "## A. Duplicate review",
        "",
        f"ROC duplicate candidate pairs reviewed: **{len(dupes)}** (no merges).",
        f"Classification counts: {dict(class_counts)}",
        "",
        "| IDs | Names | License | DBA | Class | Why |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in dupes:
        names = f"{r['id_a']} {r['name_a']} ↔ {r['id_b']} {r['name_b']}"
        lines.append(
            f"| {r['id_a']}/{r['id_b']} | {names} | {r['license']} | {r['roc_dba'] or ''} | "
            f"{r['classification']} | {', '.join(r['reasons'] or [])} |"
        )
    lines += [
        "",
        "Full per-pair snapshots (phones, projects, permits, capabilities, priority) are in",
        f"`{json_path}`. Canonicalization is a **link recommendation** only.",
        "",
        "## B. Important ambiguous entity resolution",
        "",
    ]
    for cid, note in WATCH_NOTES.items():
        row = next((x for x in cov100["rows"] if x["company_id"] == cid), None)
        extra = ""
        if row:
            extra = (
                f" rank={row.get('rank')} match={row.get('match_status')} "
                f"recommended_override={row.get('recommended_match_status')} "
                f"compact={compact_company_name(row.get('display_name'))}."
            )
        lines.append(f"- `{cid}`{extra} {note}")
    lines += [
        "",
        "Overrides are stored in `entity_match_overrides.applied=0`. "
        "`roc_company_matches` is unchanged. Fuzzy name similarity was never used to VERIFIED-upgrade.",
        "",
        "## C. Canonical-entity architecture",
        "",
        "Additive tables: `canonical_companies` → `company_entity_links` → raw `companies` → projects/permits.",
        f"Canonical rows: **{canonical_n}**. Links: **{link_n}**.",
        "Each link stores raw_company_id, canonical_company_id, relationship_type, confidence, evidence JSON,",
        "matching_version, created_at, reviewed_at.",
        "Original company names, permit contractor names, license numbers, and project FKs are untouched.",
        "Example: `GAS PIPING INC` and `GAS PIPING INC (2)` can share a canonical account while each source string remains.",
        "",
        "## D. Existing contact inventory",
        "",
        json.dumps(inventory_stats, indent=2, default=str),
        "",
        "Companies.main_phone / main_email / website were empty in production. "
        "The usable inventory is prior research CSVs + ROC addresses/QP + this official-site pass.",
        "",
        "## E. Contactability architecture",
        "",
        "`company_contact_channels` holds contact_type, value, name, original title, decision_maker_class,",
        "source_family, source_reference, discovered_at, verified_at, confidence, verification_status,",
        "is_primary, public_business_contact, notes. Inferred emails are `INFERRED_UNVERIFIED`.",
        f"Research import: {research_stats}. Official pass: {official_stats}.",
        f"Channel rows now: **{channel_n}**.",
        "",
        "## F. Top-25 contact coverage",
        "",
    ]
    lines.extend(_pct_line(cov25))
    lines += [
        "",
        f"CONTACTABLE + HIGH: {cov25['counts'].get('contactable_high', 0)}  |  "
        f"CONTACTABLE + MEDIUM: {cov25['counts'].get('contactable_medium', 0)}  |  "
        f"HIGH + NO CONTACT: {cov25['counts'].get('high_no_contact', 0)}  |  "
        f"IDENTITY AMBIGUOUS: {cov25['counts'].get('identity_ambiguous_bucket', 0)}",
        "",
        "## G. Top-100 contact coverage",
        "",
    ]
    lines.extend(_pct_line(cov100))
    lines += [
        "",
        f"CONTACTABLE + HIGH: {cov100['counts'].get('contactable_high', 0)}  |  "
        f"CONTACTABLE + MEDIUM: {cov100['counts'].get('contactable_medium', 0)}  |  "
        f"HIGH + NO CONTACT: {cov100['counts'].get('high_no_contact', 0)}  |  "
        f"IDENTITY AMBIGUOUS: {cov100['counts'].get('identity_ambiguous_bucket', 0)}",
        "",
        "## H. Decision-maker coverage",
        "",
        f"Top 25 classes: {dm25}",
        f"Top 100 classes: {dm100}",
        "Original titles are preserved. Qualifying party is not treated as a sales contact.",
        "",
        "## I. High-priority / no-contact accounts",
        "",
    ]
    if high_no:
        for r in high_no:
            lines.append(
                f"- rank {r['rank']} `{r['company_id']}` {r['display_name']} "
                f"score={r['account_priority_score']} match={r['match_status']}"
            )
    else:
        lines.append("None in the current Top 100 after the controlled public pass.")
    lines += [
        "",
        "## J. Top-25 sales-readiness table",
        "",
        "| Rank | Account | ROC | Trade | Demand | Priority | Contact | Role | Phone | Email | Web | Conf | Identity | Next | WHY NOW |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in cov25["rows"]:
        lines.append(
            "| {rank} | {display_name} | {match_status} | {trade_identity} | {primary_demand_category} | "
            "{account_priority_score} | {contact_name} | {role} | {phone} | {email} | {website} | "
            "{contact_confidence} | {match_status} | {next_action} | {why_now} |".format(
                **{k: ("" if r.get(k) is None else str(r.get(k)).replace("|", "/")) for k in r}
            )
        )
    lines += [
        "",
        "## K. Fulfillment-readiness gaps (Top 25)",
        "",
        "Lane 2 is not built. Missing fields per account:",
        "",
    ]
    for r in cov25["rows"]:
        lines.append(f"- rank {r['rank']} {r['display_name']}: {', '.join(fulfillment_gaps(r))}")
    lines += [
        "",
        "## L. Supply-sheet workflow design (not built)",
        "",
        "Identify canonical account → salesperson uses the primary public channel → contractor says",
        "'send me a supply sheet' → CorridorIQ texts/emails a tokenized HTTPS link (no account required) →",
        "contractor uploads photo/PDF/spreadsheet, types a list, or forwards an existing document →",
        "request stored as a fulfillment_request with original file + normalized lines → matched to a",
        "participating supply partner by geography/stock → partner quotes → status tracked internally",
        "(requested / received / routed / quoted / fulfilled / closed). Reduce friction: mobile-first,",
        "one tap, no password, SMS magic link, photograph-a-handwritten-list as a first-class path.",
        "",
        "## M. CRM future-state recommendation",
        "",
        "Intelligence/account-priority already expresses DISCOVERED and QUALIFIED (priority band + identity).",
        "CONTACT_READY belongs next to contactability, still outside CRM.",
        "CRM should remain actual worked relationships only, with states: CONTACTED, ENGAGED,",
        "MATERIAL_REQUESTED, QUOTE_ROUTED, CUSTOMER, DORMANT.",
        "Do not bulk-create CRM rows from the contractor directory.",
        "",
        "## N. Tests",
        "",
        "See pytest output in this phase return. New module: `pipeline/tests/test_entity_contactability.py`.",
        "",
        f"## O. Performance",
        "",
        f"Phase 4D pipeline elapsed **{elapsed_s:.1f}s**.",
        f"Guardrails before={before_guard}",
        f"Guardrails after={after_guard}",
        "Equal opportunity/relevance/priority sums, CRM count, merged count, and ROC/contact flags mean no ranking/CRM writes.",
        "",
        "## P. Recommended next step",
        "",
        "Keep ranking and dashboard off. Next useful phase is a **human identity review** of POSSIBLE/CONFLICT",
        "Top-100 accounts (Arizona Propane, Parker & Sons, Petra, Redpoint) plus a second official-site pass",
        "on remaining Top 100 gaps — still without enabling ROC ranking consumption or CRM bulk-create.",
        "",
    ]
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_GENERATED_DIR / f"entity_contactability_{ts}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
