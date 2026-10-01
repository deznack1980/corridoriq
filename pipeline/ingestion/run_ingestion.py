"""Runs ingestion for every 'connected' jurisdiction.

First run per jurisdiction defaults to a bounded lookback window rather than
a full historical backfill — Mesa's dataset alone has 150k+ rows going back
to 2003, and the dashboard only cares about recent activity. Subsequent runs
sync incrementally from `last_synced_at`.
"""

import sqlite3
import time as _time
from datetime import datetime, timedelta, timezone

from pipeline.config import settings
from pipeline.connectors.base import ConnectorNotConfiguredError, utcnow_iso
from pipeline.connectors.registry import build_connector
from pipeline.ingestion import raw_capture
from pipeline.ingestion.upsert import upsert_permit

DEFAULT_FIRST_RUN_LOOKBACK_DAYS = 730  # ~2 years


class _RawBatch:
    """RAW capture for one jurisdiction's ingest, as a context manager.

    RAW capture must never be able to break ingestion: it is an observability
    layer, not a dependency. Any failure degrades to a no-op and ingestion
    continues, because losing a day of permits is worse than losing a day of
    raw versions.
    """

    def __init__(self, conn, slug, connector_type, since):
        self.conn = conn
        self.slug = slug
        self.connector_type = connector_type
        self.since = since
        self.batch_id = None
        self.new = self.changed = self.unchanged = 0

    def __enter__(self):
        if not settings.RAW_CAPTURE_ENABLED:
            return self
        try:
            endpoint = self.conn.execute(
                "SELECT endpoint_url FROM jurisdictions WHERE slug = ?", (self.slug,)
            ).fetchone()
            self.batch_id = raw_capture.open_batch(
                self.conn,
                source_system=self.slug,
                source_entity_type=settings.RAW_ENTITY_PERMIT,
                connector_type=self.connector_type,
                source_url=endpoint["endpoint_url"] if endpoint else None,
                requested_since=self.since.isoformat() if self.since else None,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.slug}] RAW capture unavailable: {exc}")
            self.batch_id = None
        return self

    def record(self, mapped: dict) -> None:
        if self.batch_id is None:
            return
        try:
            outcome = raw_capture.capture_permit(self.conn, self.batch_id, mapped)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.slug}] RAW capture skipped a record: {exc}")
            return
        if outcome == raw_capture.NEW:
            self.new += 1
        elif outcome == raw_capture.CHANGED:
            self.changed += 1
        else:
            self.unchanged += 1

    def close(self, *, fetched: int, status: str = "succeeded",
              error_message: str | None = None) -> None:
        if self.batch_id is None:
            return
        try:
            raw_capture.close_batch(
                self.conn, self.batch_id, status=status, fetched=fetched,
                new=self.new, changed=self.changed, unchanged=self.unchanged,
                error_message=error_message,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.slug}] RAW batch close failed: {exc}")
        finally:
            self.batch_id = None

    def __exit__(self, exc_type, exc, tb):
        # Only fires when close() was not reached (an unexpected error path).
        if self.batch_id is not None:
            self.close(fetched=0, status="failed",
                       error_message=str(exc) if exc else "batch not closed")
        return False


def _parse_since(last_synced_at: str | None) -> datetime:
    """First-run fallback only: used when we hold no records for a source."""
    if last_synced_at:
        return datetime.fromisoformat(last_synced_at)
    return datetime.now(timezone.utc) - timedelta(days=DEFAULT_FIRST_RUN_LOOKBACK_DAYS)


def _held_watermark(conn: sqlite3.Connection, slug: str) -> datetime | None:
    """Newest source-assigned date we already hold for a jurisdiction."""
    row = conn.execute(
        "SELECT MAX(COALESCE(issued_date, filed_date)) AS mx "
        "FROM permits WHERE jurisdiction = ?",
        (slug,),
    ).fetchone()
    value = row["mx"] if row else None
    if not value:
        return None
    # Stored formats differ by source: plain 'YYYY-MM-DD' for most, full ISO
    # timestamps for Mesa. Only the day matters here.
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def compute_since(
    conn: sqlite3.Connection,
    slug: str,
    last_synced_at: str | None,
    lookback_days: int | None = None,
) -> datetime:
    """Watermark for a source's incremental query.

    Derived from the newest source date we already hold, minus a lookback --
    NOT from when we last ran.

    Connectors filter on the source's issue date, so a run-time watermark asks
    "give me permits issued since I last ran". Sources publish late: a permit
    issued on the 5th may not appear in the feed until the 14th. That record is
    invisible to a run watermarked the 10th, and since the watermark only ever
    advances, it can never be picked up again. The loss is permanent and
    produces no error.
    """
    lookback = timedelta(days=settings.INGEST_WATERMARK_LOOKBACK_DAYS
                         if lookback_days is None else lookback_days)
    held = _held_watermark(conn, slug)
    if held is not None:
        return held - lookback
    return _parse_since(last_synced_at)


def run_ingestion(conn: sqlite3.Connection, lookback_days: int | None = None) -> None:
    """Ingest every connected jurisdiction.

    ``lookback_days`` widens the incremental watermark for a one-off catch-up;
    it defaults to the standing window.
    """
    connected = conn.execute(
        "SELECT slug, connector_type, last_synced_at FROM jurisdictions WHERE status = 'connected'"
    ).fetchall()

    if not connected:
        print("No connected jurisdictions to ingest.")
        return

    for row in connected:
        slug = row["slug"]
        since = compute_since(conn, slug, row["last_synced_at"], lookback_days)
        run_started_at = utcnow_iso()

        print(f"[{slug}] ingesting since {since.isoformat()} ...")

        telemetry = {
            "connector_type": row["connector_type"],
            "requested_since": since.isoformat() if since else None,
        }

        try:
            connector = build_connector(slug, row["connector_type"])
        except ConnectorNotConfiguredError as exc:
            print(f"[{slug}] SKIPPED — {exc}")
            error_type, http_status = classify_error(exc)
            _record_run(conn, slug, run_started_at, 0, 0, 0, "error", str(exc),
                        error_type=error_type, http_status=http_status, **telemetry)
            continue

        # A connector failure (bad query, network error, source outage) must
        # not take down ingestion for every other jurisdiction — record it
        # and move on, same as the ConnectorNotConfiguredError path above.
        try:
            result = connector.run(since=since)
        except Exception as exc:  # noqa: BLE001
            print(f"[{slug}] FAILED — {exc}")
            conn.execute(
                "UPDATE jurisdictions SET last_sync_status = ?, last_sync_error = ? WHERE slug = ?",
                ("error", str(exc), slug),
            )
            conn.commit()
            error_type, http_status = classify_error(exc)
            _record_run(conn, slug, run_started_at, 0, 0, 0, "error", str(exc),
                        error_type=error_type, http_status=http_status, **telemetry)
            continue

        inserted = updated = 0
        with _RawBatch(conn, slug, row["connector_type"], since) as raw_batch:
            for mapped in result.records:
                # RAW first: preserve what the source said before the permit
                # row is overwritten in place.
                raw_batch.record(mapped)
                outcome = upsert_permit(conn, mapped)
                if outcome == "inserted":
                    inserted += 1
                else:
                    updated += 1
            conn.commit()

            status = "success" if not result.errors else "success_with_errors"
            error_message = "; ".join(result.errors[:5]) if result.errors else None
            raw_batch_id = raw_batch.batch_id
            raw_batch.close(fetched=result.fetched_count,
                            status="succeeded" if not result.errors else "partial",
                            error_message=error_message)
            raw_new, raw_changed = raw_batch.new, raw_batch.changed
            raw_unchanged = raw_batch.unchanged

        conn.execute(
            """
            UPDATE jurisdictions
            SET last_synced_at = ?, last_sync_status = ?, last_sync_record_count = ?, last_sync_error = ?
            WHERE slug = ?
            """,
            (utcnow_iso(), status, result.fetched_count, error_message, slug),
        )
        conn.commit()

        _record_run(
            conn, slug, run_started_at, result.fetched_count, inserted, updated,
            status, error_message,
            raw_batch_id=raw_batch_id,
            source_rows=result.source_row_count,
            duplicates_dropped=result.duplicates_dropped,
            raw_new=raw_new, raw_changed=raw_changed, raw_unchanged=raw_unchanged,
            **telemetry,
        )

        dupes = (f" dupes_dropped={result.duplicates_dropped}"
                 if result.duplicates_dropped else "")
        print(
            f"[{slug}] fetched={result.fetched_count} inserted={inserted} updated={updated} "
            f"errors={len(result.errors)} raw_new={raw_new} raw_changed={raw_changed}{dupes}"
        )

    # Health is evaluated once per ingestion pass, after every source has been
    # attempted, so a snapshot always reflects a complete round rather than a
    # partial one. Guarded: telemetry observes, it never blocks.
    from pipeline.telemetry.source_health import record_snapshot_safe

    for health in record_snapshot_safe(conn):
        if health["health_state"] not in ("healthy", "unknown"):
            print(f"[health] {health['jurisdiction_slug']}: "
                  f"{health['health_state'].upper()} - {health['detail']}")


def classify_error(exc: Exception | None) -> tuple[str | None, int | None]:
    """Reduce an exception to (error_type, http_status).

    A stored string like "500 Server Error: ... for url: https://..." cannot be
    grouped or counted. A short token can, which is what makes "this source
    fails with 5xx once a week" a query rather than an investigation.
    """
    if exc is None:
        return None, None
    if isinstance(exc, ConnectorNotConfiguredError):
        return "not_configured", None

    status = None
    try:
        import requests
    except Exception:  # pragma: no cover - connectors depend on it
        requests = None

    if requests is not None:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(exc, requests.exceptions.Timeout):
            return "timeout", status
        if isinstance(exc, requests.exceptions.ConnectionError):
            return "connection", status
        if isinstance(exc, requests.exceptions.HTTPError):
            if status is None:
                return "http_error", None
            if status == 429:
                return "rate_limited", status
            if 500 <= status < 600:
                return "http_5xx", status
            if 400 <= status < 500:
                return "http_4xx", status
            return "http_error", status
        if isinstance(exc, requests.exceptions.RequestException):
            return "network", status

    name = type(exc).__name__.lower()
    for token, label in (("timeout", "timeout"), ("connection", "connection"),
                         ("network", "network"), ("auth", "auth"),
                         ("forbidden", "auth"), ("unauthorized", "auth"),
                         ("invalid", "invalid_request"),
                         ("badrequest", "invalid_request"),
                         ("notconfigured", "not_configured")):
        if token in name:
            return label, status
    return "unknown", status


def _duration_ms(started_at: str, finished_at: str) -> int | None:
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(finished_at)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    return int((end - start).total_seconds() * 1000)


def _record_run(conn, slug, run_started_at, fetched, inserted, updated, status,
                error_message, **telemetry):
    """Append a row to ingestion_runs.

    Telemetry is best-effort: a database missing the Phase 2 columns must not
    stop ingestion from recording that the run happened at all.
    """
    finished_at = utcnow_iso()
    columns = {
        "jurisdiction_slug": slug,
        "run_started_at": run_started_at,
        "run_finished_at": finished_at,
        "records_fetched": fetched,
        "records_inserted": inserted,
        "records_updated": updated,
        "status": status,
        "error_message": error_message,
        "duration_ms": _duration_ms(run_started_at, finished_at),
    }
    columns.update({k: v for k, v in telemetry.items() if v is not None})

    try:
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO ingestion_runs ({names}) VALUES ({placeholders})",
            tuple(columns.values()),
        )
    except sqlite3.OperationalError:
        conn.execute(
            """
            INSERT INTO ingestion_runs (jurisdiction_slug, run_started_at, run_finished_at,
                                         records_fetched, records_inserted, records_updated,
                                         status, error_message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (slug, run_started_at, finished_at, fetched, inserted, updated,
             status, error_message),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Morning-refresh ingestion: per-jurisdiction, retryable, failure-isolated.
# Reuses the same connectors and upsert as the full pipeline run.
# ---------------------------------------------------------------------------

class _NonRetryable(Exception):
    """Wraps an error that must NOT be retried (auth / invalid request)."""


def is_retryable_error(exc: Exception) -> bool:
    """Retry only transient network/server faults. Never retry authentication
    or invalid-request (4xx) failures, or configuration problems."""
    if isinstance(exc, ConnectorNotConfiguredError):
        return False
    try:
        import requests  # local import: connectors depend on it, tests may not
    except Exception:  # pragma: no cover
        requests = None

    if requests is not None:
        if isinstance(exc, (requests.exceptions.Timeout,
                            requests.exceptions.ConnectionError,
                            requests.exceptions.ChunkedEncodingError)):
            return True
        if isinstance(exc, requests.exceptions.HTTPError):
            resp = getattr(exc, "response", None)
            code = getattr(resp, "status_code", None)
            # 429 + 5xx are transient; 401/403 (auth) and 400/404 (invalid) are not.
            return code in (429, 500, 502, 503, 504)
        if isinstance(exc, requests.exceptions.RequestException):
            return True
    # Fall back to name-based detection so tests can raise lightweight stand-ins.
    name = type(exc).__name__.lower()
    if any(tok in name for tok in ("auth", "forbidden", "unauthorized",
                                   "invalid", "badrequest", "notconfigured")):
        return False
    return any(tok in name for tok in ("timeout", "connection", "temporary",
                                       "unavailable", "network"))


def _fetch_with_retry(connector, since, *, attempts, backoff_base, backoff_max, sleep):
    """Run the connector, retrying transient failures with exponential backoff.

    Returns (ConnectorResult, retries_used). Raises the last exception when all
    attempts are exhausted, or immediately for non-retryable errors.
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return connector.run(since=since), attempt - 1
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not is_retryable_error(exc) or attempt >= attempts:
                raise
            delay = min(backoff_base * (2 ** (attempt - 1)), backoff_max)
            sleep(delay)
    raise last_exc  # pragma: no cover - loop always returns or raises


def _latest_source_dates(conn: sqlite3.Connection, slug: str) -> dict:
    row = conn.execute(
        """
        SELECT MAX(filed_date)  AS newest_submitted,
               MAX(issued_date) AS newest_issued,
               MAX(COALESCE(issued_date, filed_date, finaled_date)) AS newest_any
        FROM permits WHERE jurisdiction = ?
        """,
        (slug,),
    ).fetchone()
    return {
        "newest_submitted_date": row["newest_submitted"],
        "newest_issued_date": row["newest_issued"],
        "newest_source_date": row["newest_any"],
    }


def ingest_jurisdiction(
    conn: sqlite3.Connection,
    slug: str,
    connector_type: str,
    last_synced_at: str | None,
    *,
    attempts: int | None = None,
    backoff_base: float | None = None,
    backoff_max: float | None = None,
    lookback_days: int | None = None,
    sleep=_time.sleep,
) -> dict:
    """Ingest a single jurisdiction with retry + failure isolation.

    Always returns a stats dict (never raises for a source failure) so the
    caller can continue with the other jurisdictions. Detects unchanged records
    so only genuinely new/changed permits bump ``last_updated_at``.
    """
    attempts = attempts or settings.MORNING_RETRY_MAX_ATTEMPTS
    backoff_base = settings.MORNING_RETRY_BACKOFF_SECONDS if backoff_base is None else backoff_base
    backoff_max = settings.MORNING_RETRY_BACKOFF_MAX_SECONDS if backoff_max is None else backoff_max

    since = compute_since(conn, slug, last_synced_at, lookback_days)
    run_started_at = utcnow_iso()
    stats = {
        "slug": slug,
        "status": "failed",
        "fetched": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "submitted_only": 0,
        "retries": 0,
        "error": None,
        "raw_new": 0,
        "raw_changed": 0,
        "duplicates_dropped": 0,
        "source_rows": 0,
    }

    telemetry = {
        "connector_type": connector_type,
        "requested_since": since.isoformat() if since else None,
    }

    try:
        connector = build_connector(slug, connector_type)
    except ConnectorNotConfiguredError as exc:
        stats["error"] = str(exc)
        conn.execute(
            "UPDATE jurisdictions SET last_sync_status = ?, last_sync_error = ? WHERE slug = ?",
            ("error", str(exc), slug),
        )
        conn.commit()
        error_type, http_status = classify_error(exc)
        _record_run(conn, slug, run_started_at, 0, 0, 0, "error", str(exc),
                    error_type=error_type, http_status=http_status, **telemetry)
        stats.update(_latest_source_dates(conn, slug))
        stats["error_type"] = error_type
        return stats

    try:
        result, retries = _fetch_with_retry(
            connector, since, attempts=attempts, backoff_base=backoff_base,
            backoff_max=backoff_max, sleep=sleep,
        )
    except Exception as exc:  # noqa: BLE001
        stats["error"] = f"{type(exc).__name__}: {exc}"
        conn.execute(
            "UPDATE jurisdictions SET last_sync_status = ?, last_sync_error = ? WHERE slug = ?",
            ("error", str(exc), slug),
        )
        conn.commit()
        error_type, http_status = classify_error(exc)
        # attempts - 1 is the retry budget; an exhausted budget means every
        # retry was spent, which is worth recording separately from the error.
        _record_run(conn, slug, run_started_at, 0, 0, 0, "error", str(exc),
                    error_type=error_type, http_status=http_status,
                    retries=attempts - 1 if error_type != "not_configured" else 0,
                    **telemetry)
        stats.update(_latest_source_dates(conn, slug))
        stats["error_type"] = error_type
        return stats

    inserted = updated = unchanged = 0
    with _RawBatch(conn, slug, connector_type, since) as raw_batch:
        for mapped in result.records:
            # RAW first: preserve what the source said before the permit row
            # is overwritten in place.
            raw_batch.record(mapped)
            outcome = upsert_permit(conn, mapped, detect_unchanged=True)
            if outcome == "inserted":
                inserted += 1
            elif outcome == "updated":
                updated += 1
            else:
                unchanged += 1
        conn.commit()

        status = "success" if not result.errors else "success_with_errors"
        error_message = "; ".join(result.errors[:5]) if result.errors else None
        raw_batch_id = raw_batch.batch_id
        raw_batch.close(fetched=result.fetched_count,
                        status="succeeded" if not result.errors else "partial",
                        error_message=error_message)
        raw_new, raw_changed = raw_batch.new, raw_batch.changed
        raw_unchanged = raw_batch.unchanged

    conn.execute(
        """
        UPDATE jurisdictions
        SET last_synced_at = ?, last_sync_status = ?, last_sync_record_count = ?, last_sync_error = ?
        WHERE slug = ?
        """,
        (utcnow_iso(), status, result.fetched_count, error_message, slug),
    )
    conn.commit()
    _record_run(conn, slug, run_started_at, result.fetched_count, inserted, updated,
                status, error_message,
                raw_batch_id=raw_batch_id,
                source_rows=result.source_row_count,
                duplicates_dropped=result.duplicates_dropped,
                records_unchanged=unchanged, retries=retries,
                raw_new=raw_new, raw_changed=raw_changed, raw_unchanged=raw_unchanged,
                **telemetry)

    stats.update({
        "status": status,
        "fetched": result.fetched_count,
        "inserted": inserted,
        "updated": updated,
        "unchanged": unchanged,
        "retries": retries,
        "error": error_message,
        "raw_new": raw_new,
        "raw_changed": raw_changed,
        "duplicates_dropped": result.duplicates_dropped,
        "source_rows": result.source_row_count,
    })
    stats.update(_latest_source_dates(conn, slug))
    return stats
