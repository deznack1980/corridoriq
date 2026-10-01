"""Candidate actions. Thresholds are judgments. Measured numbers come from the snapshot."""

from __future__ import annotations

from agents.ceo.decision_engine import evidence as ev
from agents.framework.provenance import is_known, value_of

# Documented in charter/operating_principles.md. Used only when the latest
# sales-trial report does not state its own actionable-contact target.
FALLBACK_CONTACT_TARGET = 80


def classify_proposal(question: str) -> str:
    text = question.lower()
    if _cosmetic(text):
        return "cosmetic_score"
    if _intake(text):
        return "intake_photos"
    if _fulfillment(text):
        return "fulfillment_marketplace"
    return "open"


def _cosmetic(text: str) -> bool:
    mentions_score = (
        "account_priority_score" in text or "account priority score" in text
    )
    vanity = any(
        phrase in text
        for phrase in (
            "because we like",
            "look better",
            "make the demo",
            "rank #1",
            "rank 1",
            "becomes rank",
        )
    )
    return mentions_score and vanity


def _intake(text: str) -> bool:
    field = any(
        phrase in text
        for phrase in (
            "trial shows",
            "consistently want",
            "field evidence",
            "customers said",
            "we learned",
        )
    )
    medium = any(
        phrase in text for phrase in ("photo", "handwritten", "text photos", "text a photo")
    )
    return field and medium


def _fulfillment(text: str) -> bool:
    topic = "fulfillment" in text or "marketplace" in text
    build = any(
        phrase in text for phrase in ("build", "building", "spend", "implement", "week")
    )
    return topic and build and not _intake(text)


def contact_target(pct_metric: dict) -> float | None:
    if not is_known(pct_metric):
        return None
    stated = pct_metric.get("target")
    if stated is None:
        return float(FALLBACK_CONTACT_TARGET)
    return float(stated)


def trial_ready(snapshot: dict) -> bool:
    sales = snapshot["sales"]
    pct = sales["actionable_public_contact_pct"]
    accounts = sales["learning_trial_accounts"]
    outcomes = sales["recorded_field_outcomes"]
    if not (is_known(pct) and is_known(accounts) and is_known(outcomes)):
        return False
    target = contact_target(pct)
    if target is None or float(pct["value"]) < target:
        return False
    names = accounts.get("value")
    if not isinstance(names, list) or not names:
        return False
    return value_of(outcomes) == 0


def account_names(snapshot: dict) -> list[str]:
    item = snapshot["sales"]["learning_trial_accounts"]
    if is_known(item) and isinstance(item.get("value"), list):
        return [str(name) for name in item["value"]]
    return []


def select(snapshot: dict, question: str) -> dict:
    proposal = classify_proposal(question)
    ready = trial_ready(snapshot)
    if proposal == "cosmetic_score":
        return _reject_score(snapshot, question)
    if proposal == "intake_photos":
        return _intake_prototype(snapshot, question)
    if proposal == "fulfillment_marketplace":
        return _refuse_fulfillment_build(snapshot, question, ready)
    if ready:
        return _learning_trial(snapshot, question)
    return _insufficient(snapshot, question)


def _learning_trial(snapshot: dict, question: str) -> dict:
    names = account_names(snapshot)
    listed = ", ".join(names) if names else "the accounts named in the latest sales-trial artifact"
    sales = snapshot["sales"]
    return {
        "action_id": "run_learning_trial",
        "kind": "VALIDATE",
        "recommendation": (
            f"Run the prepared learning trial. Call {listed} and log an outcome for each account."
        ),
        "bottleneck": "CUSTOMER_VALIDATION",
        "bottleneck_evidence": [
            "The frozen cohort meets the contactability bar in the latest sales-trial artifact.",
            "Named trial accounts are already prepared.",
            "Recorded field outcomes are zero, so customer reaction is still unknown.",
        ],
        "why_it_matters": (
            "Another scoring or dashboard pass would spend engineering before CorridorIQ knows "
            "whether these accounts buy the inferred materials or will share a supply list."
        ),
        "what_removes_it": (
            "A logged outcome for every prepared account, including no-answer, plus at least one "
            "conversation that captures buyer role, current supplier, or accepted list format."
        ),
        "why": _why_trial(snapshot),
        "do_now": [
            f"Call the prepared accounts ({listed}) from the internal call sheet.",
            "Ask who buys, who supplies them today, and whether they will send a list by text, photo, email, or PDF.",
            "Write one outcome per account. Do not bulk-create CRM rows from the ranked book.",
        ],
        "do_not": [
            "Rewrite opportunity_score, customer relevance, or account_priority_score.",
            "Cut over the customer dashboard or enable ROC ranking consumption.",
            "Build a fulfillment marketplace, add autonomous agents, or expand ACC/UCC ingestion.",
        ],
        "success_criteria": (
            "Each prepared account has a logged outcome, and at least one live conversation "
            "records buyer role, current supplier, or the list format they will actually send."
        ),
        "next_gate": (
            "If two or more accounts confirm the same list format or a concrete buying pain, "
            "consider a constrained experiment. If the contacts or the demand are wrong, "
            "fix that evidence before more engineering."
        ),
        "confidence": "HIGH",
        "unknowns": [
            "Customer reaction to the prepared accounts is UNKNOWN until outcomes are logged.",
            "Revenue is UNKNOWN.",
            "Approved pricing is UNKNOWN.",
            "Latest full-suite test status was not collected by this snapshot.",
        ],
        "alternatives": [
            {"action": "score_rewrite", "disposition": "rejected", "reason": "Contactability is not the bottleneck."},
            {"action": "dashboard_cutover", "disposition": "rejected", "reason": "No cutover authorization and no field evidence."},
            {"action": "fulfillment_marketplace", "disposition": "rejected", "reason": "Product B is an unvalidated hypothesis."},
            {"action": "more_agents", "disposition": "rejected", "reason": "An agent does not replace the calls."},
        ],
        "engineering": False,
        "rule": "WHEN THE NEXT BEST ACTION IS SELLING OR CALLING CUSTOMERS, SAY SO",
        "evidence": ev.compact(
            [
                ev.from_metric("Actionable public contact", sales["actionable_public_contact_pct"]),
                ev.from_metric("Verified contact", sales["verified_contact_pct"]),
                ev.from_metric("Callability", sales["callability"]),
                ev.from_metric("Learning-trial accounts", sales["learning_trial_accounts"]),
                ev.from_metric("Recorded field outcomes", sales["recorded_field_outcomes"]),
                ev.from_metric(
                    "Fulfillment implemented",
                    snapshot["product"]["fulfillment_implemented"],
                ),
                ev.from_metric(
                    "Dashboard cutover authorized",
                    snapshot["product"]["dashboard_cutover_authorized"],
                ),
            ev.hypothesis(
                "Product A (subscription intelligence for supply houses) is the current focus. "
                "Product B (contractor demand / fulfillment engine) is a separate future hypothesis. "
                "They are not blended, and no marketplace is assumed.",
                "agents/ceo/knowledge/commercial_models.md",
            ),
            ]
        ),
        "problem": question,
    }


def _why_trial(snapshot: dict) -> list[str]:
    sales = snapshot["sales"]
    product = snapshot["product"]
    lines = []
    pct = sales["actionable_public_contact_pct"]
    if is_known(pct):
        lines.append(
            f"[VERIFIED_SYSTEM_STATE] Actionable public contact on the frozen cohort is "
            f"{_fmt_pct(pct)} (source {pct.get('source')}). "
            f"The contactability bar used here is {contact_target(pct):g}%."
        )
    verified = sales["verified_contact_pct"]
    if is_known(verified):
        lines.append(
            f"[VERIFIED_SYSTEM_STATE] Verified contact coverage is {_fmt_pct(verified)}."
        )
    phones = sales["business_phone_pct"]
    emails = sales["business_email_pct"]
    sites = sales["website_pct"]
    named = sales["named_decision_maker_pct"]
    if all(is_known(item) for item in (phones, emails, sites, named)):
        lines.append(
            "[VERIFIED_SYSTEM_STATE] Business phone "
            f"{_fmt_pct(phones)}, email {_fmt_pct(emails)}, website {_fmt_pct(sites)}, "
            f"named decision maker {_fmt_pct(named)}."
        )
    purchasing = sales["purchasing_ops_count"]
    if is_known(purchasing):
        lines.append(
            "[VERIFIED_SYSTEM_STATE] Named purchasing, estimating, or operations contacts: "
            f"{purchasing.get('value')} of {purchasing.get('n')}. "
            "That gap is a question for the calls, not a reason to retune scores."
        )
    outcomes = sales["recorded_field_outcomes"]
    if is_known(outcomes):
        lines.append(
            f"[VERIFIED_SYSTEM_STATE] Recorded field outcomes: {outcomes.get('value')}. "
            f"{outcomes.get('note') or ''}".strip()
        )
    fulfillment = product["fulfillment_implemented"]
    if is_known(fulfillment):
        state = "implemented" if fulfillment.get("value") else "not implemented"
        lines.append(f"[VERIFIED_SYSTEM_STATE] Fulfillment is {state}.")
    elif not is_known(fulfillment):
        lines.append("[UNKNOWN] Fulfillment implementation status was not in the artifact.")
    dashboard = product["dashboard_cutover_authorized"]
    if is_known(dashboard) and dashboard.get("value") is False:
        lines.append("[VERIFIED_SYSTEM_STATE] Customer dashboard cutover is not authorized.")
    lanes = snapshot["intelligence"]["plumbing_lane_separated"]
    if is_known(lanes) and lanes.get("value") is True:
        lines.append(
            "[VERIFIED_SYSTEM_STATE] PLUMBING_CORE separation is a presentation layer. "
            "It does not replace account_priority_score."
        )
    roc = snapshot["intelligence"]["roc_role"]
    if is_known(roc):
        lines.append(
            f"[VERIFIED_SYSTEM_STATE] ROC role in the latest validation report: {roc.get('value')}."
        )
    lines.append(
        "[INTERNAL_HYPOTHESIS] Product A, subscription intelligence for supply houses, is the "
        "current focus and is not yet validated revenue. Product B, a contractor demand / "
        "fulfillment engine, is a separate future hypothesis and is not built."
    )
    lines.append(
        "[MODEL_INFERENCE] Engineering is not the bottleneck while a prepared, contactable "
        "trial has no logged outcomes."
    )
    return lines


def _refuse_fulfillment_build(snapshot: dict, question: str, ready: bool) -> dict:
    if ready:
        base = _learning_trial(snapshot, question)
        base["rule"] = "VALIDATE BEFORE AUTOMATING"
        base["why"] = [
            "[INTERNAL_HYPOTHESIS] Fulfillment / demand routing is not a validated commercial model.",
            "[FIELD_EVIDENCE] No logged contractor conversations in this snapshot show demand for a routed marketplace.",
            *base["why"],
        ]
        base["challenge_proposal"] = "fulfillment_marketplace"
        return base
    return {
        "action_id": "manual_demand_test",
        "kind": "VALIDATE",
        "recommendation": (
            "Do not build the fulfillment marketplace. Run the smallest manual test that "
            "shows whether contractors will send a real supply request."
        ),
        "bottleneck": "CUSTOMER_VALIDATION",
        "bottleneck_evidence": [
            "The build request assumes Product B demand that is not in the field log.",
        ],
        "why_it_matters": "A marketplace encodes workflow, pricing, and partner economics that are still unknown.",
        "what_removes_it": "Manual evidence of a repeated request format and a real recipient.",
        "why": [
            "[INTERNAL_HYPOTHESIS] Product B is unvalidated.",
            "[UNKNOWN] Contractor workflow preferences, supply-list formats, and partner economics.",
        ],
        "do_now": [
            "Write the question the manual test must answer.",
            "Use existing public contacts only if a prepared cohort exists; otherwise stop and collect that cohort.",
            "Log the result before any routing software is specified.",
        ],
        "do_not": [
            "Spend the week implementing a fulfillment marketplace.",
            "Treat a readiness label in an internal audit as a product.",
            "Change ranking scores to make the marketplace story look further along.",
        ],
        "success_criteria": "A written record of real request formats from contractors, or a clear refusal.",
        "next_gate": "Only a repeated, real request format reopens a software discussion, and then only for a narrow intake test.",
        "confidence": "MEDIUM",
        "unknowns": [
            "Fulfillment demand is UNKNOWN.",
            "Supply-list formats are UNKNOWN.",
            "Partner economics are UNKNOWN.",
        ],
        "alternatives": [
            {"action": "build_fulfillment_marketplace", "disposition": "rejected", "reason": "Hypothesis, not evidence."},
        ],
        "engineering": False,
        "rule": "DO NOT BUILD WHAT A MANUAL TEST CAN VALIDATE FIRST",
        "evidence": [
            ev.hypothesis(
                "Fulfillment demand routing is not validated.",
                "agents/ceo/knowledge/commercial_models.md",
            )
        ],
        "problem": question,
        "challenge_proposal": "fulfillment_marketplace",
    }


def _intake_prototype(snapshot: dict, question: str) -> dict:
    return {
        "action_id": "constrained_intake_prototype",
        "kind": "BUILD",
        "recommendation": (
            "Run a constrained intake experiment for texted photos of handwritten supply lists. "
            "Do not authorize a fulfillment marketplace."
        ),
        "bottleneck": "PRODUCT",
        "bottleneck_evidence": [
            "Founder-reported field evidence says contractors want to text photos of handwritten lists.",
            "No product path exists to capture that format, and a full routing system is not justified by it.",
        ],
        "why_it_matters": (
            "The new uncertainty is whether CorridorIQ can capture a real list in the format "
            "contractors already use. Partner routing is a later decision."
        ),
        "what_removes_it": (
            "Several real photos captured and readable by a salesperson, with a written success "
            "or failure against a pre-set count."
        ),
        "why": [
            "[FIELD_EVIDENCE] The question reports that the trial showed a consistent preference for texted photos of handwritten lists. Confidence is MEDIUM until the outcome log contains the same fact.",
            "[INTERNAL_HYPOTHESIS] A marketplace, partner pricing, and normalized order routing are still hypotheses.",
            "[MODEL_INFERENCE] The proportionate build is a reversible intake prototype, not a platform.",
        ],
        "do_now": [
            "Confirm the claim against notes from the prepared accounts before writing production code.",
            "Define the prototype as: receive a photo, store it internally, and let a person read it.",
            "Set a stop if the photos are unreadable or only one account will use the format.",
        ],
        "do_not": [
            "Build partner routing, quote workflow, or a fulfillment marketplace.",
            "Change account_priority_score, relevance, opportunity scores, or sales lanes.",
            "Cut over the customer dashboard or enable ROC ranking.",
        ],
        "success_criteria": (
            "At least three real supply-list photos are captured through the experiment and a "
            "salesperson can extract the requested items without a marketplace."
        ),
        "next_gate": (
            "If three readable photos arrive and the senders want a repeatable path, specify "
            "the next slice. If they do not, stop and keep the learning in the decision log."
        ),
        "confidence": "MEDIUM",
        "unknowns": [
            "The photo claim is not yet a logged call outcome.",
            "Partner economics are UNKNOWN.",
            "Quote and order workflow requirements are UNKNOWN.",
        ],
        "alternatives": [
            {"action": "full_fulfillment_marketplace", "disposition": "rejected", "reason": "The evidence is about a list format, not a market."},
            {"action": "manual_photo_inbox", "disposition": "parallel", "reason": "Keep receiving photos by hand while the prototype stays narrow."},
            {"action": "score_rewrite", "disposition": "rejected", "reason": "The new evidence is not a ranking error."},
        ],
        "engineering": True,
        "rule": "DO NOT BUILD WHAT A MANUAL TEST CAN VALIDATE FIRST",
        "evidence": ev.compact(
            [
                ev.founder_claim(question),
                ev.hypothesis(
                    "Fulfillment routing beyond intake is unvalidated.",
                    "agents/ceo/knowledge/commercial_models.md",
                ),
                ev.from_metric(
                    "Recorded field outcomes in the operating snapshot",
                    snapshot["sales"]["recorded_field_outcomes"],
                ),
            ]
        ),
        "problem": question,
        "challenge_proposal": "intake_photos",
    }


def _reject_score(snapshot: dict, question: str) -> dict:
    return {
        "action_id": "reject_cosmetic_score_change",
        "kind": "STOP",
        "recommendation": (
            "STOP. Do not change account_priority_score to promote a favored account. "
            "Rank stays on the evidence already in the score."
        ),
        "bottleneck": "CUSTOMER_VALIDATION" if trial_ready(snapshot) else "UNKNOWN",
        "bottleneck_evidence": [
            "The request is a presentation preference, not a demonstrated scoring error.",
            "The operating bottleneck is unchanged by moving one liked account to rank 1.",
        ],
        "why_it_matters": (
            "Cosmetic retunes teach the team to distrust the score and hide data-quality "
            "problems inside a friendlier sort."
        ),
        "what_removes_it": (
            "Leave the score untouched. If a later trial shows a systematic miss, bring that "
            "class of error back as evidence."
        ),
        "why": [
            "[MODEL_INFERENCE] Rank should follow evidence. A preference for one account is not a validation result.",
            "[VERIFIED_SYSTEM_STATE] account_priority_score and sales-lane fit are different systems. Lane presentation can shape a book when the lane evidence supports it. It is not a tool for favoring an account.",
            "[INTERNAL_HYPOTHESIS] Model weights should change only when validation evidence shows a systematic error.",
            "[MODEL_INFERENCE] Customer-specific relevance belongs in sales-lane presentation, not in a one-row score edit.",
        ],
        "do_now": [
            "Leave account_priority_score, customer relevance, and opportunity_score unchanged.",
            "If the account's lane fit is actually wrong, review lane evidence separately and do not touch the score.",
            "Return to the learning trial if it is prepared and still has no logged outcomes.",
        ],
        "do_not": [
            "Edit weights or a single score so a preferred account becomes rank 1.",
            "Hide the current rank with a dashboard sort that pretends the score changed.",
            "Treat this request as customer evidence.",
        ],
        "success_criteria": (
            "account_priority_score is unchanged and the account's rank remains the evidence rank."
        ),
        "next_gate": (
            "Reopen scoring only with logged outcomes that show a repeated, systematic error, "
            "not a single favored name."
        ),
        "confidence": "HIGH",
        "unknowns": [
            "No field evidence in this request shows the current rank is systematically wrong.",
        ],
        "alternatives": [
            {"action": "retune_account_priority_score", "disposition": "rejected", "reason": "Cosmetic."},
            {"action": "sales_lane_presentation", "disposition": "available_if_evidence", "reason": "Use only when lane fit, not favoritism, is the issue."},
            {"action": "run_learning_trial", "disposition": "preferred_if_ready", "reason": "Field evidence outranks a demo sort."},
        ],
        "engineering": False,
        "rule": "DO NOT RETUNE MODELS MERELY TO MAKE DEMOS LOOK BETTER",
        "evidence": [
            ev.inference("The request states a preference, not a measured scoring error."),
            ev.hypothesis(
                "Score weights move only on systematic validation error.",
                "agents/ceo/charter/operating_principles.md",
            ),
        ],
        "problem": question,
        "challenge_proposal": "cosmetic_score",
    }


def _insufficient(snapshot: dict, question: str) -> dict:
    return {
        "action_id": "collect_evidence",
        "kind": "RESEARCH",
        "recommendation": (
            "Do not pick a build or a sales push yet. The snapshot is missing the cohort "
            "evidence required to name a bottleneck."
        ),
        "bottleneck": "UNKNOWN",
        "bottleneck_evidence": [
            "Actionable contact coverage, prepared trial accounts, or recorded outcomes are UNKNOWN.",
        ],
        "why_it_matters": "Choosing a project from missing metrics would hide the gap.",
        "what_removes_it": "A readable sales-trial artifact or a read-only database cohort with those fields.",
        "why": [
            "[UNKNOWN] One or more of actionable contact coverage, trial accounts, and field outcomes is unavailable.",
            "[MODEL_INFERENCE] No percentage or account list is being filled in from memory.",
        ],
        "do_now": [
            "Locate the latest sales_trial JSON and markdown.",
            "Re-run the CEO brief against that artifact.",
            "If the database is required for a count, read it with the read-only collector.",
        ],
        "do_not": [
            "Invent coverage, revenue, or pipeline health.",
            "Start a scoring rewrite while the bottleneck is UNKNOWN.",
            "Authorize fulfillment or a dashboard cutover.",
        ],
        "success_criteria": "A new snapshot where the missing cohort fields are KNOWN or explicitly still absent.",
        "next_gate": "When those fields are KNOWN, run the brief again.",
        "confidence": "LOW",
        "unknowns": list(snapshot.get("unknowns") or [])[:8] or ["Cohort evidence is UNKNOWN."],
        "alternatives": [
            {"action": "run_learning_trial", "disposition": "blocked", "reason": "Trial readiness is not known."},
        ],
        "engineering": False,
        "rule": "EVIDENCE OVER ASSUMPTION",
        "evidence": ev.compact(
            [
                ev.from_metric(
                    "Actionable public contact",
                    snapshot["sales"]["actionable_public_contact_pct"],
                ),
                ev.from_metric(
                    "Learning-trial accounts",
                    snapshot["sales"]["learning_trial_accounts"],
                ),
            ]
        ),
        "problem": question,
        "challenge_proposal": "open",
    }


def _fmt_pct(item: dict) -> str:
    value = item.get("value")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "UNKNOWN"
    shown = str(int(number)) if number.is_integer() else str(number)
    if item.get("count") is not None and item.get("n"):
        return f"{shown}% ({item['count']}/{item['n']})"
    return f"{shown}%"
