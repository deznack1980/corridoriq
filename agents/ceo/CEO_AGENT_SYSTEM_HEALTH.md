# CEO system health

Health status is computed in `agents/ceo/analytics/health.py`. A model is not asked whether CorridorIQ is healthy. Narration cannot change a failed check into a pass. `execute` stays false. `automatic_remediation_permitted` is false on every incident. This check does not repair, migrate, vacuum, or reindex the database, and it does not send email, SMS, or webhooks.

## Checks

| Check | Critical | Rule |
| --- | --- | --- |
| Morning refresh | yes | The newest `morning_refresh` row wins. `failed` is critical. `partial` is a warning. Missing history is critical. `succeeded` is healthy only when `completed_at` is within 36 hours of the check. An older success does not hide a newer failure. |
| Database | yes | The file must open read-only. A permit count and the sum of succeeded `records_received` must be non-negative. If that sum is above zero and `permits` is empty, the check is critical. No integrity rebuild is run. |
| Schema | yes | `permits`, `projects`, `companies`, `pipeline_runs`, `jurisdictions`, `source_health_snapshot`, `users`, and `sessions` must exist. |
| Feeds | no | Connected jurisdictions use `get_feed_freshness`. Stale means `days_since_newest_source` > 45. Thin means stale and `records_7d` is 0. Stored `health_state` of failing, degraded, or silent is a warning. |
| Downstream analytics | yes | `get_pipeline_health`, `get_feed_freshness`, `get_top_opportunities`, and `get_contact_gaps` must return row lists. A query failure is critical. One ranking snapshot stays UNKNOWN and is not a failure. |
| Brief publication | yes, after a succeeded refresh | Added by the publisher. Success is healthy. Failure after a succeeded refresh is critical. Skipped after a failed or partial refresh is not applicable. |
| Application | yes | `COUNT(*)` on `users` and `sessions` must succeed. No login and no public HTTP call. |
| Billing | no | `NOT_ENABLED` / not applicable until the Stripe branch is integrated. It does not call Stripe and does not change overall health. |

## Feed anomaly thresholds

These are inferences, not facts that the source is broken. The fact is the stored `records_7d`.

- Anomalous zero: latest `records_7d` is 0, and the previous 4 snapshots each have `records_7d` >= 5. Severity: warning.
- Insufficient baseline: latest `records_7d` is 0 and that history is missing. The check stays UNKNOWN. It is not an incident and does not by itself create an owner alert.
- Material drop: latest `records_7d` is below half the median of the previous 4 snapshots, and that median is at least 10. Severity: warning.
- Fewer than 4 prior snapshots means the drop rule does not fire.

## Overall status

1. Any check with status `critical` → `critical` (CRITICAL — ACTION REQUIRED).
2. Else any critical check with status `unknown` → `unknown` (CHECK INCOMPLETE — REVIEW NEEDED).
3. Else any `warning` → `warning` (WARNING — REVIEW NEEDED).
4. Else `healthy` (ALL SYSTEMS HEALTHY).

`not_applicable`, including billing, does not change the result. A non-critical UNKNOWN, such as a feed with no baseline, does not block ALL SYSTEMS HEALTHY.

## Owner alert

`owner_alert_required` is true for warning, critical, and unknown. It is false for healthy. The fields `alert_reason`, `alert_severity`, and `incident_count` are the future delivery contract. v0.3 writes `CEO_OUTPUT_DIR/intelligence/latest_health_status.json` and `history/health-refresh-<id>.json`. It does not send the alert.

## What this will not do

It will not rerun a refresh, repair a database, edit scores, call Stripe, or send a message. A future billing check can replace the `billing` check after the Stripe branch is merged. Until then the status remains NOT_ENABLED.
