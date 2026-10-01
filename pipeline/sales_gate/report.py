"""Phase 4E internal report. Not a customer or dashboard surface."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import REPORTS_GENERATED_DIR, ROC_DATA_DIR, SALES_GATE_VERSION
from pipeline.entity.report import score_guardrails
from pipeline.sales_gate.audit import scorecard
from pipeline.sales_gate.identity import IDENTITY_CASES

DECISION = "B"
DECISION_LABEL = "READY FOR INTERNAL SALES VIEW ONLY"


def _pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def write_phase4e_report(
    conn: sqlite3.Connection,
    *,
    accounts: list[dict],
    identity_stats: dict,
    person_stats: dict,
    official_stats: dict,
    elapsed_s: float,
    before_guard: dict,
    after_guard: dict,
) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    sc = scorecard(accounts)
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    ROC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_GENERATED_DIR / f"sales_readiness_{ts}.md"
    snap = ROC_DATA_DIR / f"sales_readiness_{ts}.json"
    payload = {
        "version": SALES_GATE_VERSION,
        "decision": DECISION,
        "scorecard": {k: v for k, v in sc.items() if not isinstance(v, list)},
        "accounts": accounts,
        "identity": identity_stats,
        "persons": person_stats,
        "official": official_stats,
        "guardrails_before": before_guard,
        "guardrails_after": after_guard,
        "elapsed_s": elapsed_s,
    }
    snap.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def names(rows):
        return ", ".join(f"{r['rank']}. {r['display_name']}" for r in rows) or "(none)"

    lines = [
        f"# CorridorIQ Phase 4E — Top-25 sales readiness ({ts})",
        "",
        f"Model `{SALES_GATE_VERSION}`. Internal validation gate. Ranking, opportunity_score,",
        "customer_relevance_score, account_priority_score, CRM, ROC ranking consumption,",
        "and company merges were **not** changed.",
        "",
        f"## L. Dashboard readiness decision: **{DECISION}. {DECISION_LABEL}**",
        "",
        "Not A — a salesperson can act today on a real core of fuel-gas and plumbing contractors",
        "with evidence-based WHY NOW, observed demand, and public phones.",
        "Not C — a founding partner would still see person-name pool applicants, a permit",
        "expeditor, fire-protection houses, GCs, and a duplicate RCI row inside the Top 25.",
        "Not D — no customer UI, ranking still unfiltered, ROC internals not production-consumed.",
        "What blocks C: ranks 10/21/24 (Espinoza, Rider, Sekona); fire/GC/civil occupying plumber",
        "slots; RCI duplicated at 3 and 5; production matcher still POSSIBLE/CONFLICT on",
        "Arizona Propane, Parker, Petra; purchasing/estimator coverage still thin.",
        "",
        "## K. Product quality scorecard (Top 25)",
        "",
        f"- SALES_READY: {sc['pct_sales_ready']}%",
        f"- SALES_READY_WITH_CAUTION: {sc['pct_caution']}%",
        f"- RESEARCH_FIRST: {sc['pct_research']}%",
        f"- NOT_SALES_READY: {sc['pct_not_ready']}%",
        f"- Identity VERIFIED/HIGH_CONFIDENCE: {sc['pct_identity_high']}%",
        f"- Actionable phone: {sc['pct_phone']}%",
        f"- Business email: {sc['pct_email']}%",
        f"- Website: {sc['pct_website']}%",
        f"- Named decision-maker: {sc['pct_named']}%",
        f"- Useful buyer/estimator/ops contact: {sc['pct_useful_role']}%",
        f"- Credible WHY NOW: {sc['pct_why']}%",
        f"- Identifiable material-demand category: {sc['pct_demand']}%",
        f"- Ready for fulfillment outreach: {sc['pct_fulfillment']}%",
        f"- Intelligence answers all five questions: {sc['pct_all_five']}%",
        "",
        "These are product-quality metrics. They are not account_priority_score.",
        "",
        "## A. Identity resolution results",
        "",
    ]
    for cid, case in IDENTITY_CASES.items():
        lines += [
            f"### {case['display_name']} (`{cid}`)",
            f"- Canonical: {case['canonical_account']}",
            f"- Legal: {case['legal_entity']}  |  DBA: {case['dba']}",
            f"- ROC: {case['roc_licenses']}  |  class: {case['roc_classes']}",
            f"- Address: {case['business_address']}",
            f"- Website: {case['official_website']}  |  phone: {case['official_phone']}",
            f"- Evidence: {', '.join(case['match_evidence'])}",
            f"- Remaining: {case['remaining_conflicts']}",
            f"- **Status: {case['recommended_identity_status']}**",
            "",
        ]
    lines += [
        f"Identity reviews written: {identity_stats}. `roc_company_matches` applied=0.",
        "",
        "## B. Contact gap results",
        "",
        json.dumps(official_stats, indent=2, default=str),
        "",
        "Umbrella Plumbing: official site tryumbrellaplumbing.com, 480-869-6952,",
        "office@tryumbrellaplumbing.com, 3434 N San Marcos Pl Chandler. VERIFIED.",
        "Hiller Companies: hillerfire.com Phoenix line 888-222-0532 and contact form. VERIFIED.",
        "Hiller is fire protection, not a plumbing Top-25 occupant (rank 30).",
        "Rider Permit Service: no official website, no ROC license. Directory numbers were",
        "**not** stored as verified. NOT_SALES_READY.",
        "",
        "## C. Person-name account findings",
        "",
        json.dumps(person_stats, indent=2, default=str),
        "",
        "Kris Espinoza and Stephanie Sekona: unmatched individuals on Mesa swimming-pool /",
        "PE gas-line permits. Permit applicants, not licensed sole proprietors. NOT_SALES_READY.",
        "Martha Nava, Katie Weinmann, Mark Nieves: person-name shower/wet-space remodels.",
        "Nihya Moscoso: person name on a new-residence plumbing permit.",
        "Mike Shriver, Fabian Torres, Michelle Shuck: pool/gas-line permit names.",
        "Jacuzzi Bath & Remodel: `looks_like_person_name` false positive (FLAG detector; do not retune).",
        "No private personal numbers were collected. Accounts were not deleted.",
        "",
        "## D. Top-25 sales-readiness table",
        "",
        "| Rk | Company | Pri | Identity | Segment | Ready | Phone | WHY NOW |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for r in accounts:
        why = (r["sales_why_now"] or "").replace("|", "/")[:140]
        lines.append(
            f"| {r['rank']} | {r['display_name']} | {r['account_priority_score']} | "
            f"{r['identity_status']} | {r['account_segment']} | {r['sales_readiness']} | "
            f"{r['phone'] or ''} | {why} |"
        )
    lines += [
        "",
        "Full per-account fields (demand, contacts, current job, fulfillment, flags) are in",
        f"`{snap}`.",
        "",
        "## E. WHY NOW quality",
        "",
        f"{sc['pct_why']}% of the Top 25 have evidence-based sales_why_now (permits, job counts,",
        "license class, current description). Ranking `why_now` is still the generic identity+",
        "window string and was **not** overwritten.",
        "",
        "## F. Material-demand / likely-buy",
        "",
    ]
    for r in accounts:
        lines.append(
            f"- {r['rank']}. {r['display_name']}: {r['demand_evidence_level']} — {r['likely_buy']}"
        )
    lines += [
        "",
        "## G. Top-25 segmentation (no rerank)",
        "",
        json.dumps(sc["segments"], indent=2),
        "",
        "## H. Supply-house sales simulation",
        "",
        f"A. Immediately callable: {names(sc['immediately_callable'])}",
        f"B. Identity verification first: {names(sc['identity_first'])}",
        f"C. Lack a useful contact: {names(sc['no_contact'])}",
        f"D. Likely buying plumbing/gas/wet-side: {names(sc['plumbing_gas_wet'])}",
        f"E. Other sales segment: {names(sc['other_segment'])}",
        f"F. Should not occupy scarce Top-25 attention: {names(sc['should_not_occupy'])}",
        "",
        "## I. Fulfillment-lane readiness",
        "",
    ]
    by_f = {}
    for r in accounts:
        by_f.setdefault(r["fulfillment_readiness"], []).append(r)
    for k, rs in sorted(by_f.items()):
        lines.append(f"- {k}: {names(rs)}")
    lines += [
        "",
        "No fulfillment workflow was built.",
        "",
        "## J. Intelligence-lane readiness",
        "",
        "Questions: WHO / WHY NOW / WHAT demand / HOW to reach / WHICH projects.",
        f"All five: {names(sc['all_five_rows'])}",
        f"Missing at least one: {names(sc['missing_intel'])}",
        "",
        "## M. Minimum account-card design (not built)",
        "",
        "See the Phase 4E canvas mockup using three real Top-25 accounts. Do not expose ROC",
        "internals, weights, source-family codes, or raw confidence formulas.",
        "",
        "## N. Test results / guardrails",
        "",
        f"- elapsed: {elapsed_s}s",
        f"- before: {before_guard}",
        f"- after:  {after_guard}",
        f"- guardrails_unchanged: {before_guard == after_guard}",
        "",
        "## O. Remaining blockers",
        "",
        "1. Person-name and permit-expeditor rows still rank inside Top 25.",
        "2. Fire, GC/CM, civil, and landscape-gas accounts occupy plumber slots.",
        "3. RCI Systems is listed twice (ranks 3 and 5).",
        "4. Production ROC matcher still POSSIBLE/CONFLICT on Arizona Propane, Parker, Petra, Redpoint.",
        "5. Purchasing / estimator named coverage remains thin.",
        "6. Ranking `why_now` is still generic; sales_why_now is overlay-only.",
        "7. Dashboard ranking is unchanged and would ship the mixed book as-is.",
        "",
        "## P. Recommended next step",
        "",
        "Do **not** retune weights to pretty the Top 25. Propose a filtered internal sales view",
        "that hides NOT_SALES_READY and optionally demotes other-segment accounts, after explicit",
        "approval. Then resolve RCI duplicate display (canonical view, not a destructive merge).",
        "",
        "STOP. Dashboard, ranking, CRM, and fulfillment were not implemented.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
