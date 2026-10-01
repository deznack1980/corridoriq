"""Phase 4G internal report. Not a customer surface."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pipeline.config.settings import REPORTS_GENERATED_DIR, SALES_TRIAL_VERSION
from pipeline.sales_trial.callability import CALL_NOW, CALL_WITH_CAUTION, NO_ACTIONABLE_CONTACT, RESEARCH_MORE
from pipeline.sales_trial.outcomes import schema_spec


def write_phase4g_report(
    *,
    frozen: list[dict],
    inventory: dict,
    enrich: dict,
    before: dict,
    after: dict,
    callability_rows: list[dict],
    delta: dict,
    abc: dict,
    trial: dict,
    html_path: Path,
    elapsed_s: float,
    before_guard: dict,
    after_guard: dict,
    missing: list[dict],
) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    REPORTS_GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_GENERATED_DIR / f"sales_trial_{ts}.md"
    def _pct(block, key):
        return f"{block['counts'].get(key, 0)}/{block['n']} = {block['pct'].get(key, 0)}%"

    call_counts = {}
    for rec in callability_rows:
        st = rec.get("callability") or "UNKNOWN"
        call_counts[st] = call_counts.get(st, 0) + 1

    frozen_lines = []
    for rec in frozen:
        frozen_lines.append(
            f"- #{rec['presentation_rank']} {rec['canonical_name']} "
            f"(id={rec['primary_company_id']} members={rec['member_company_ids']}) "
            f"priority={rec['account_priority_score']} identity={rec['identity_status']} "
            f"lane={rec['quality_label']} contact={rec['contact_confidence']} "
            f"actionable={bool(rec['actionable'])}"
        )

    after_lines = []
    for rec in callability_rows:
        a = rec.get("after") or {}
        after_lines.append(
            f"- #{rec['presentation_rank']} {rec['canonical_name']}: "
            f"{rec.get('callability')} | {a.get('contact_confidence')} | "
            f"phone={a.get('phone') or '—'} email={a.get('email') or '—'} "
            f"web={a.get('website') or '—'} named={a.get('contact_name') or '—'} "
            f"({rec.get('callability_reasons')})"
        )

    trial_lines = []
    for card in trial.get("accounts") or []:
        bc = card.get("best_contact") or {}
        trial_lines.append(
            f"""### {card['company']}
- WHY NOW: {card.get('why_now')}
- LIKELY MATERIAL DEMAND: {card.get('likely_material_demand')}
- RECENT ACTIVITY 30/90/180: {card['recent_activity']['30d']} / {card['recent_activity']['90d']} / {card['recent_activity']['180d']}
- BEST CONTACT: {bc.get('name')} · {bc.get('role')} · {bc.get('phone')} · {bc.get('email')} · {bc.get('website')} ({bc.get('confidence')})
- CURRENT PROJECT: {card.get('current_project') or '—'}
- OPENING OBJECTIVE: {card.get('opening_objective')}
- CALLABILITY: {card.get('callability')}
- CRM: {card.get('crm_status') or 'none'}
"""
        )

    learn = "\n".join(f"- {q}" for q in trial.get("learning_objectives") or [])
    missing_lines = "\n".join(
        f"- #{m['presentation_rank']} {m['canonical_name']}: {m.get('reason')}"
        for m in missing
    ) or "- none"

    payload = {
        "model_version": SALES_TRIAL_VERSION,
        "before_kpis": before,
        "after_kpis": after,
        "callability_counts": call_counts,
        "delta": {k: delta.get(k) for k in ("linked", "merged_companies", "combined_frozen_pair") if k in delta},
        "abc_present_as": abc.get("present_as"),
        "guardrails": {"before": before_guard, "after": after_guard},
        "outcomes_design": schema_spec(),
    }
    json_path = REPORTS_GENERATED_DIR / f"sales_trial_{ts}.json"
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    md = f"""# Phase 4G — Plumbing-core contact enrichment + internal sales trial

INTERNAL ONLY. Scores were not recalculated. Frozen Top 25 membership/order were not rebuilt.

Elapsed: {elapsed_s}s · model `{SALES_TRIAL_VERSION}` · call sheet: `{html_path}`

## A. Frozen Top-25 baseline

{chr(10).join(frozen_lines)}

## B. Existing contact inventory (before new research)

Accounts with any stored channel: {inventory.get('accounts_with_any_channel')} / {len(frozen)}

Prior sources already on file for the cohort were used first (company_contact_channels, prior research CSVs, official catalogs). Companies/contacts tables for this cohort were empty in Phase 4D.

## C. Contact enrichment results

Official catalog apply: {enrich}

## D. Before / after contact coverage

| KPI | Before | After |
|---|---|---|
| business phone | {_pct(before, 'has_phone')} | {_pct(after, 'has_phone')} |
| business email | {_pct(before, 'has_email')} | {_pct(after, 'has_email')} |
| website | {_pct(before, 'has_website')} | {_pct(after, 'has_website')} |
| contact form | {_pct(before, 'has_form')} | {_pct(after, 'has_form')} |
| named decision-maker | {_pct(before, 'has_named')} | {_pct(after, 'has_named')} |
| purchasing/estimating/operations | {_pct(before, 'has_purchasing_ops')} | {_pct(after, 'has_purchasing_ops')} |
| owner/principal | {_pct(before, 'has_owner')} | {_pct(after, 'has_owner')} |
| >=1 actionable channel | {_pct(before, 'actionable')} | {_pct(after, 'actionable')} |
| VERIFIED contact | {_pct(before, 'verified')} | {_pct(after, 'verified')} |
| CANDIDATE-only | {_pct(before, 'candidate_only')} | {_pct(after, 'candidate_only')} |
| no-contact | {_pct(before, 'no_contact')} | {_pct(after, 'no_contact')} |

Target: >=80% actionable if a public business channel can be found. Do not fabricate.

## E. Decision-maker coverage

See after named / owner / purchasing-ops KPIs above. Original titles were preserved; titles were not invented.

## F. Arizona Delta resolution

Linked: {delta.get('linked')} · merged companies: {delta.get('merged_companies')}

{json.dumps(delta.get('combined_frozen_pair'), indent=2, default=str)}

## G. ABC Water Works lane recommendation

{abc.get('recommendation')}

Present as: {abc.get('present_as')}

company_sales_lanes unchanged: {abc.get('lanes_unchanged')}

## H. Top-25 callability

{json.dumps(call_counts)}

{chr(10).join(after_lines)}

## I. Accounts still missing contacts

{missing_lines}

## J. Four-account internal sales trial

Replacements: {trial.get('replacements') or 'none'}

{chr(10).join(trial_lines)}

## K. Internal call-sheet view

Generated HTML: `{html_path}`

## L. Sales-learning objectives

{learn}

## M. Future call-outcome schema

DESIGN ONLY. {schema_spec()['outcomes']}

Learnings: {schema_spec()['learning_fields']}

## N. Intelligence product implications

Supply-house account books can now carry a public contact layer (phone/email/web/form + named roles) without changing ranking. Callability is a presentation badge, not a score.

## O. Fulfillment product implications

The same contact layer is what a future material-list / routing motion would use. Fulfillment is not implemented.

## P. Test results

See pytest output from this phase.

## Q. Guardrail check

Before: {before_guard}

After: {after_guard}

## R. Recommended next step

Run a four-account learning trial from the internal call sheet. Do not cut over the customer dashboard, bulk-create CRM, or build fulfillment.
"""
    path.write_text(md, encoding="utf-8")
    return path
