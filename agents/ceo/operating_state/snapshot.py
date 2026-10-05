"""Serializable CEO operating snapshot. No secrets and no invented metrics."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from agents.ceo.operating_state.collector import collect_database, resolve_db_path
from agents.ceo.paths import reports_generated_dir
from agents.ceo.retrieval.artifacts import load_sales_artifacts
from agents.framework.provenance import (
    INTERNAL_HYPOTHESIS,
    UNKNOWN,
    is_known,
    metric,
    unknown,
    value_of,
)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _prefer_db(db_item: dict, artifact_item: dict) -> dict:
    if is_known(db_item) and is_known(artifact_item) and db_item.get("value") != artifact_item.get("value"):
        merged = dict(db_item)
        merged["note"] = (
            f"Database value {db_item.get('value')} differs from "
            f"{artifact_item.get('source')} value {artifact_item.get('value')}."
        )
        return merged
    if is_known(db_item):
        return db_item
    return artifact_item


def _latest_report_names(reports_dir: Path) -> dict:
    prefixes = (
        "sales_trial_",
        "sales_lanes_",
        "sales_readiness_",
        "entity_contactability_",
        "roc_identity_",
        "account_priority_",
        "customer_relevance_",
        "contractor_intel_",
    )
    if not reports_dir.exists():
        return unknown("reports directory is missing", source="reports/generated")
    found = []
    for prefix in prefixes:
        matches = sorted(reports_dir.glob(prefix + "*.md")) + sorted(
            reports_dir.glob(prefix + "*.json")
        )
        if matches:
            found.append(matches[-1].name)
    if not found:
        return unknown("no CEO-relevant generated reports", source="reports/generated")
    return metric(found, source="reports/generated", category="VERIFIED_SYSTEM_STATE")


def _walk_unknowns(node, prefix: str, found: list[str]) -> None:
    if isinstance(node, dict) and "status" in node and "value" in node and set(node) >= {
        "status",
        "value",
    }:
        if node.get("status") == UNKNOWN:
            found.append(prefix)
        return
    if isinstance(node, dict):
        for key, child in node.items():
            child_prefix = f"{prefix}.{key}" if prefix else key
            _walk_unknowns(child, child_prefix, found)


def build_snapshot(
    *,
    reports_dir: Path | None = None,
    db_path: Path | None = None,
    connect_db: bool = True,
    generated_at: str | None = None,
) -> dict:
    reports = reports_dir if reports_dir is not None else reports_generated_dir()
    database = collect_database(resolve_db_path(db_path) if connect_db else None)
    artifacts = load_sales_artifacts(reports)
    art = artifacts.get("metrics") or {}

    def art_or_unknown(name: str, note: str) -> dict:
        return art.get(name) or unknown(note, source=artifacts.get("source"))

    source_health = database["source_health"]
    source_failures = unknown("source health was not readable")
    if is_known(source_health) and isinstance(source_health.get("value"), dict):
        states = source_health["value"]
        if not states:
            source_failures = unknown(
                "no source_health_snapshot rows", source="sqlite-readonly"
            )
        else:
            source_failures = metric(
                int(states.get("failing", 0)),
                source="sqlite-readonly",
                category="VERIFIED_SYSTEM_STATE",
                note="silent and degraded are reported separately in source_health",
                silent=int(states.get("silent", 0)),
                degraded=int(states.get("degraded", 0)),
            )

    roc_ranking = art_or_unknown("guard_roc_enabled", "ROC enable flag unavailable")
    if is_known(database["roc_enabled"]):
        roc_ranking = _prefer_db(database["roc_enabled"], roc_ranking)

    snapshot = {
        "generated_at": generated_at or _now(),
        "agent_version": "0.1.0",
        "output_class": "INTERNAL",
        "data_health": {
            "project_count": _prefer_db(
                database["project_count"],
                art_or_unknown("guard_projects", "project count unavailable"),
            ),
            "permit_count": database["permit_count"],
            "company_count": database["company_count"],
            "source_freshness": source_health,
            "pipeline_health": database["latest_pipeline_run"],
            "source_failures": source_failures,
        },
        "intelligence": {
            "contractor_intel_companies": database["contractor_intel_companies"],
            "relevance_rows": _prefer_db(
                database["relevance_rows"],
                art_or_unknown("guard_relevance_rows", "relevance coverage unavailable"),
            ),
            "account_priority_rows": _prefer_db(
                database["priority_rows"],
                art_or_unknown("guard_priority_rows", "account-priority coverage unavailable"),
            ),
            "sales_lane_counts": database["sales_lane_counts"],
            "identity_review_count": database["identity_review_count"],
            "roc_ranking_enabled": roc_ranking,
            "roc_role": art_or_unknown("roc_role", "ROC role unavailable"),
            "plumbing_lane_separated": art_or_unknown(
                "plumbing_lane_separated", "sales-lane separation unavailable"
            ),
        },
        "sales": {
            "cohort_size": art_or_unknown("cohort_size", "frozen cohort size unavailable"),
            "actionable_public_contact_pct": art_or_unknown(
                "actionable_public_contact_pct", "actionable contact coverage unavailable"
            ),
            "verified_contact_pct": art_or_unknown(
                "verified_contact_pct", "verified contact coverage unavailable"
            ),
            "business_phone_pct": art_or_unknown(
                "business_phone_pct", "business phone coverage unavailable"
            ),
            "business_email_pct": art_or_unknown(
                "business_email_pct", "business email coverage unavailable"
            ),
            "website_pct": art_or_unknown("website_pct", "website coverage unavailable"),
            "named_decision_maker_pct": art_or_unknown(
                "named_decision_maker_pct", "named decision-maker coverage unavailable"
            ),
            "purchasing_ops_count": art_or_unknown(
                "purchasing_ops_count", "purchasing-contact count unavailable"
            ),
            "no_actionable_contact_count": art_or_unknown(
                "no_actionable_contact_count", "no-contact count unavailable"
            ),
            "callability": art_or_unknown("callability", "callability unavailable"),
            "learning_trial_accounts": art_or_unknown(
                "learning_trial_accounts", "learning-trial accounts unavailable"
            ),
            "recorded_field_outcomes": _prefer_db(
                database["recorded_field_outcomes"],
                art_or_unknown(
                    "recorded_field_outcomes", "field outcomes unavailable"
                ),
            ),
            "trial_accounts_with_crm": art_or_unknown(
                "trial_accounts_with_crm", "trial CRM status unavailable"
            ),
            "crm_relationships": _prefer_db(
                database["crm_relationships"],
                art_or_unknown("guard_crm_relationships", "CRM count unavailable"),
            ),
        },
        "product": {
            "latest_test_status": unknown(
                "CEO v0.1 does not execute the CorridorIQ test suite during a snapshot"
            ),
            "latest_reports": _latest_report_names(reports),
            "dashboard_cutover_authorized": art_or_unknown(
                "dashboard_cutover_authorized", "dashboard cutover authorization unavailable"
            ),
            "fulfillment_implemented": art_or_unknown(
                "fulfillment_implemented", "fulfillment implementation status unavailable"
            ),
        },
        "commercial": {
            "revenue": unknown("no revenue ledger is available to the CEO agent"),
            "pricing": unknown("no approved price is on file"),
            "pilot_status": _pilot_status(art),
            "product_a": metric(
                "supplier intelligence for supply houses; closest current path to revenue; not validated",
                source="agents/ceo/knowledge/commercial_models.md",
                category=INTERNAL_HYPOTHESIS,
                confidence="MEDIUM",
            ),
            "product_b": metric(
                "contractor procurement; EARLY_ACCESS typed material request in pilot code; marketplace not authorized",
                source="agents/ceo/knowledge/commercial_models.md",
                category=INTERNAL_HYPOTHESIS,
                confidence="MEDIUM",
            ),
            "products_separate": metric(
                True,
                source="agents/ceo/knowledge/commercial_models.md",
                category=INTERNAL_HYPOTHESIS,
                note="Concepts stay distinct. Procurement connects them. Do not blend them into a marketplace.",
            ),
            "assume_marketplace": metric(
                False,
                source="agents/ceo/knowledge/commercial_models.md",
                category=INTERNAL_HYPOTHESIS,
            ),
            "assume_product_winner": metric(
                False,
                source="agents/ceo/knowledge/commercial_models.md",
                category=INTERNAL_HYPOTHESIS,
            ),
        },
        "known_blockers": [],
        "recent_decisions": [],
        "unknowns": [],
    }
    snapshot["known_blockers"] = _blockers(snapshot)
    ignored = art.get("ignored_newer_reports")
    if ignored:
        snapshot["sales"]["ignored_newer_reports"] = ignored
        snapshot["known_blockers"].append(
            "A newer sales-trial file had a zero project guardrail and was not used as the operating cohort: "
            + ", ".join(ignored.get("value") or [])
        )
    unknowns: list[str] = []
    _walk_unknowns(snapshot, "", unknowns)
    snapshot["unknowns"] = unknowns
    return snapshot


def _pilot_status(art: dict) -> dict:
    accounts = art.get("learning_trial_accounts")
    outcomes = art.get("recorded_field_outcomes")
    if is_known(accounts) and is_known(outcomes):
        count = len(value_of(accounts) or [])
        if count and value_of(outcomes) == 0:
            return metric(
                "prepared_not_started",
                source=accounts.get("source"),
                category="VERIFIED_SYSTEM_STATE",
                timestamp=accounts.get("timestamp"),
                note="trial accounts are named and no call outcomes are recorded in the artifact",
            )
        if count and value_of(outcomes):
            return metric(
                "outcomes_recorded",
                source=outcomes.get("source"),
                category="VERIFIED_SYSTEM_STATE",
                timestamp=outcomes.get("timestamp"),
            )
    return unknown("pilot status cannot be derived from the available artifacts")


def _blockers(snapshot: dict) -> list[str]:
    blockers = []
    sales = snapshot["sales"]
    product = snapshot["product"]
    outcomes = sales["recorded_field_outcomes"]
    if is_known(outcomes) and value_of(outcomes) == 0 and is_known(sales["learning_trial_accounts"]):
        blockers.append("Learning trial is prepared and no field outcomes are recorded.")
    purchasing = sales["purchasing_ops_count"]
    if is_known(purchasing) and value_of(purchasing) == 0:
        blockers.append(
            "Named purchasing, estimating, or operations contacts are zero on the frozen cohort."
        )
    missing = sales["no_actionable_contact_count"]
    if is_known(missing) and value_of(missing):
        blockers.append(
            f"{value_of(missing)} frozen-cohort account(s) have no actionable public contact."
        )
    fulfillment = product["fulfillment_implemented"]
    if is_known(fulfillment) and value_of(fulfillment) is False:
        blockers.append("Fulfillment is not implemented.")
    dashboard = product["dashboard_cutover_authorized"]
    if is_known(dashboard) and value_of(dashboard) is False:
        blockers.append("Customer dashboard cutover is not authorized.")
    failures = snapshot["data_health"]["source_failures"]
    if is_known(failures) and value_of(failures):
        blockers.append(f"{value_of(failures)} source(s) are in the failing health state.")
    return blockers
