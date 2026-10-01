"""Append-only RAW capture of source observations (data platform Phase 1).

Called immediately BEFORE ``upsert_permit`` so that whatever the source said is
preserved even though the permit row itself is still updated in place. Nothing
in the application reads this layer yet - it exists to stop the bleeding while
the curated layer is designed.

Why hashing matters here: the pipeline currently re-``UPDATE``s rows that did
not change (Goodyear's 245 real records produced 12,250 recorded updates across
54 runs, because that connector has no incremental date filter). Comparing a
payload hash against the current version means an unchanged re-fetch writes
nothing at all, so ``records_changed`` finally means "genuinely changed".

Contract:
    * ``raw_record`` is INSERT-only for payloads. A stored ``payload_json`` is
      never altered.
    * Two UPDATEs are permitted, both on derived bookkeeping rather than data:
      flipping ``is_current`` 1 -> 0 when superseded, and correcting a stale
      ``payload_hash`` after the hashing rule changes.
    * Nothing is ever deleted.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from pipeline.config import settings

NEW = "new"
CHANGED = "changed"
UNCHANGED = "unchanged"

# Source-internal fields excluded from CHANGE DETECTION only. The full payload
# is always stored verbatim; these keys simply must not make a record look
# changed when no business data moved.
#
# Found empirically: Chandler's MapServer returns two rows per permit number
# that are identical across all 13 business fields and differ only in
# OBJECTID (an Esri row id that is reassigned between requests). Hashing the
# whole payload made all 85 Chandler permits churn two versions per run.
_HASH_EXCLUDED_KEYS = frozenset({
    # Esri row identity and geometry bookkeeping
    "objectid", "fid", "oid", "esri_oid", "globalid",
    "shape", "shape_length", "shape_area", "shape__length", "shape__area",
    # Esri editor tracking
    "created_date", "created_user", "creationdate", "creator",
    "last_edited_date", "last_edited_user", "editdate", "editor",
})

# Bump whenever _HASH_EXCLUDED_KEYS or the canonicalisation changes.
#   1 = sha256 of the whole payload
#   2 = sha256 of the payload minus source-internal keys
HASH_VERSION = 2


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _significant(payload):
    """Drop source-internal keys before hashing.

    Socrata system fields are all ':'-prefixed (':id', ':version',
    ':updated_at', ':@computed_region_*') and are excluded as a family.
    """
    if not isinstance(payload, dict):
        return payload
    return {
        k: v
        for k, v in payload.items()
        if not (str(k).startswith(":") or str(k).lower() in _HASH_EXCLUDED_KEYS)
    }


def _as_object(payload):
    """Parse a payload into a dict/list where possible, else return it as text."""
    if isinstance(payload, (dict, list)):
        return payload
    text = "" if payload is None else str(payload)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return text
    return parsed if isinstance(parsed, (dict, list)) else text


def canonical_payload(payload) -> str:
    """Stable text form of the FULL payload, for storage.

    Key order and whitespace must not register as a change, so dicts are
    re-serialised with sorted keys. A payload that is not valid JSON is kept
    as-is rather than dropped - storing something opaque beats storing nothing.
    """
    obj = _as_object(payload)
    if isinstance(obj, (dict, list)):
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return obj


def payload_hash(payload) -> str:
    """Hash of the business-meaningful subset of a payload."""
    obj = _significant(_as_object(payload))
    if isinstance(obj, (dict, list)):
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    else:
        text = obj
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Source-side modification timestamps, where a source states one unambiguously.
# Deliberately narrow: a wrong source_updated_at is worse than a NULL one, so
# nothing is inferred from fields whose meaning was not verified.
_SOURCE_UPDATED_KEYS = (":updated_at",)


def extract_source_updated_at(payload) -> str | None:
    """Return the source's own 'last modified' value, or None.

    Only Socrata's ':updated_at' is read today; it is an explicit, documented
    system field. Esri layers expose editor-tracking fields inconsistently and
    are left alone until verified per jurisdiction.
    """
    obj = _as_object(payload)
    if not isinstance(obj, dict):
        return None
    for key in _SOURCE_UPDATED_KEYS:
        value = obj.get(key)
        if value:
            return str(value)
    return None


# ---------------------------------------------------------------------------
# Batch lifecycle
# ---------------------------------------------------------------------------

def open_batch(
    conn: sqlite3.Connection,
    source_system: str,
    source_entity_type: str,
    *,
    connector_type: str | None = None,
    source_url: str | None = None,
    requested_since: str | None = None,
    pipeline_run_id: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO raw_ingest_batch (pipeline_run_id, source_system, source_entity_type,
                                      connector_type, source_url, requested_since,
                                      started_at, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'running')
        """,
        (pipeline_run_id, source_system, source_entity_type, connector_type,
         source_url, requested_since, _now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def close_batch(
    conn: sqlite3.Connection,
    batch_id: int,
    *,
    status: str = "succeeded",
    fetched: int = 0,
    new: int = 0,
    changed: int = 0,
    unchanged: int = 0,
    error_message: str | None = None,
) -> None:
    conn.execute(
        """
        UPDATE raw_ingest_batch
           SET completed_at = ?, status = ?, records_fetched = ?, records_new = ?,
               records_changed = ?, records_unchanged = ?, error_message = ?
         WHERE batch_id = ?
        """,
        (_now(), status, fetched, new, changed, unchanged, error_message, batch_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------

def capture(
    conn: sqlite3.Connection,
    batch_id: int,
    source_system: str,
    source_record_id: str,
    payload,
    *,
    source_entity_type: str = settings.RAW_ENTITY_PERMIT,
    source_url: str | None = None,
    source_updated_at: str | None = None,
    fetched_at: str | None = None,
) -> str:
    """Record one observation. Returns 'new', 'changed' or 'unchanged'.

    Writes nothing when the payload hash matches the current version.
    """
    if not source_record_id:
        # A record with no natural id cannot be versioned or traced back, and
        # silently inventing a key would corrupt the lineage this layer exists
        # to provide.
        return UNCHANGED

    digest = payload_hash(payload)
    current = conn.execute(
        """
        SELECT raw_record_id, payload_hash, payload_hash_version, version_number,
               payload_json
          FROM raw_record
         WHERE source_system = ? AND source_record_id = ? AND is_current = 1
        """,
        (source_system, str(source_record_id)),
    ).fetchone()

    if current is not None and current["payload_hash_version"] != HASH_VERSION:
        # The hashing rule changed. Re-derive the stored payload's hash under
        # the current rule and correct it in place, so a rule change does not
        # manufacture a version bump for every record in the corpus.
        current_digest = payload_hash(current["payload_json"])
        conn.execute(
            "UPDATE raw_record SET payload_hash = ?, payload_hash_version = ? "
            "WHERE raw_record_id = ?",
            (current_digest, HASH_VERSION, current["raw_record_id"]),
        )
        current = dict(current)
        current["payload_hash"] = current_digest

    if current is not None and current["payload_hash"] == digest:
        return UNCHANGED

    if current is None:
        version, outcome = 1, NEW
    else:
        version, outcome = current["version_number"] + 1, CHANGED
        # Clear the old current first: a partial unique index allows only one
        # is_current = 1 row per record, so the insert would otherwise fail.
        conn.execute(
            "UPDATE raw_record SET is_current = 0 WHERE raw_record_id = ?",
            (current["raw_record_id"],),
        )

    conn.execute(
        """
        INSERT INTO raw_record (batch_id, source_system, source_entity_type,
                                source_record_id, payload_json, payload_hash,
                                payload_hash_version, source_url,
                                source_updated_at, fetched_at,
                                version_number, is_current)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        """,
        (batch_id, source_system, source_entity_type, str(source_record_id),
         canonical_payload(payload), digest, HASH_VERSION, source_url,
         source_updated_at, fetched_at or _now(), version),
    )
    return outcome


def capture_permit(conn: sqlite3.Connection, batch_id: int, mapped: dict,
                   **kwargs) -> str:
    """Capture a mapped permit dict's raw payload.

    ``mapped['raw_source_json']`` is the verbatim source record attached by
    ``BaseConnector.run``; the permit number is its natural id within the
    jurisdiction, matching the permits table's own UNIQUE key.
    """
    payload = mapped.get("raw_source_json")
    kwargs.setdefault("source_updated_at", extract_source_updated_at(payload))
    return capture(
        conn,
        batch_id,
        source_system=mapped.get("jurisdiction"),
        source_record_id=mapped.get("permit_number"),
        payload=payload,
        source_entity_type=settings.RAW_ENTITY_PERMIT,
        source_url=mapped.get("permit_url"),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Read helpers (diagnostics; the application does not depend on these)
# ---------------------------------------------------------------------------

def current_version(conn: sqlite3.Connection, source_system: str,
                    source_record_id: str):
    return conn.execute(
        "SELECT * FROM raw_record WHERE source_system = ? AND source_record_id = ? "
        "AND is_current = 1",
        (source_system, str(source_record_id)),
    ).fetchone()


def version_history(conn: sqlite3.Connection, source_system: str,
                    source_record_id: str) -> list:
    return list(conn.execute(
        "SELECT * FROM raw_record WHERE source_system = ? AND source_record_id = ? "
        "ORDER BY version_number",
        (source_system, str(source_record_id)),
    ))


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS versions,
               COUNT(DISTINCT source_system || '|' || source_record_id) AS records,
               MIN(fetched_at) AS earliest,
               MAX(fetched_at) AS latest
          FROM raw_record
        """
    ).fetchone()
    return dict(row) if row else {}
