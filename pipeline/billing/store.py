"""billing_accounts / billing_stripe_events persistence."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

# Serializes Customer creation, Checkout creation and webhook application in
# this process, so a double click or two concurrent deliveries of one event
# cannot create duplicate Stripe Customers or interleave state writes.
# Across processes the database itself is the guard: billing_accounts writes
# are compare-and-set on `version` (ConcurrentBillingUpdate on conflict), the
# Stripe Customer is only ever set when absent, and event IDs are claimed with
# INSERT OR IGNORE on a primary key.
BILLING_LOCK = threading.RLock()


class ConcurrentBillingUpdate(Exception):
    """Another writer changed this billing row first; re-read and retry."""

_ACCOUNT_COLUMNS = frozenset({
    "stripe_customer_id", "stripe_subscription_id", "stripe_price_id",
    "subscription_status", "billing_state", "livemode", "current_period_end",
    "cancel_at_period_end", "checkout_session_id", "checkout_expires_at",
    "billing_updated_at",
})
_DONE_PREFIXES = ("applied", "ignored", "rejected")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def epoch_to_iso(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromtimestamp(int(value), timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def get_account(conn: sqlite3.Connection, organization_id: int):
    return conn.execute("SELECT * FROM billing_accounts WHERE organization_id=?",
                        (organization_id,)).fetchone()


def ensure_account(conn: sqlite3.Connection, organization_id: int):
    now = now_iso()
    conn.execute("INSERT OR IGNORE INTO billing_accounts (organization_id, billing_state, created_at, updated_at) "
                 "VALUES (?, 'none', ?, ?)", (organization_id, now, now))
    conn.commit()
    return get_account(conn, organization_id)


def account_by_customer(conn: sqlite3.Connection, customer_id: str):
    if not customer_id:
        return None
    return conn.execute("SELECT * FROM billing_accounts WHERE stripe_customer_id=?", (customer_id,)).fetchone()


def update_account(conn: sqlite3.Connection, organization_id: int, *, expect_version: int | None = None,
                   **fields) -> None:
    """Write billing fields. With `expect_version`, the write only happens if the
    row is still at that version (raises ConcurrentBillingUpdate otherwise)."""
    bad = set(fields) - _ACCOUNT_COLUMNS
    if bad:
        raise ValueError(f"unknown billing column(s): {sorted(bad)}")
    if not fields:
        return
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    sql = f"UPDATE billing_accounts SET {cols}, version=version+1 WHERE organization_id=?"
    params = [*fields.values(), organization_id]
    if expect_version is not None:
        sql += " AND version=?"
        params.append(expect_version)
    cur = conn.execute(sql, params)
    conn.commit()
    if expect_version is not None and cur.rowcount != 1:
        raise ConcurrentBillingUpdate(f"billing row for organization {organization_id} changed concurrently")


def set_customer_if_absent(conn: sqlite3.Connection, organization_id: int, customer_id: str) -> str:
    """Store the Stripe Customer only if none is stored yet; return the stored one
    (which may be another writer's, identical when the idempotency key matched)."""
    conn.execute("UPDATE billing_accounts SET stripe_customer_id=?, version=version+1, updated_at=? "
                 "WHERE organization_id=? AND stripe_customer_id IS NULL",
                 (customer_id, now_iso(), organization_id))
    conn.commit()
    return get_account(conn, organization_id)["stripe_customer_id"]


_LIVE_STATE_VALUES = ("incomplete", "trialing", "active", "past_due", "unpaid", "paused", "unknown")


def record_checkout_session(conn: sqlite3.Connection, organization_id: int, session_id, expires_at) -> None:
    """Remember the open Checkout Session in one atomic statement. The state only
    becomes checkout_pending if no live subscription is recorded *at write time*,
    so a concurrently applied activation can never be overwritten."""
    live = ",".join("?" for _ in _LIVE_STATE_VALUES)
    conn.execute(
        "UPDATE billing_accounts SET checkout_session_id=?, checkout_expires_at=?, "
        f"billing_state=CASE WHEN stripe_subscription_id IS NULL OR billing_state NOT IN ({live}) "
        "THEN 'checkout_pending' ELSE billing_state END, version=version+1, updated_at=? WHERE organization_id=?",
        (session_id, expires_at, *_LIVE_STATE_VALUES, now_iso(), organization_id))
    conn.commit()


def begin_event(conn: sqlite3.Connection, event_id: str, event_type: str, livemode: bool) -> str:
    """Claim a Stripe event. Returns 'new', 'retry' (an earlier attempt errored)
    or 'duplicate' (already handled — do nothing)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO billing_stripe_events (event_id, event_type, livemode, outcome, received_at) "
        "VALUES (?, ?, ?, 'processing', ?)", (event_id, event_type, 1 if livemode else 0, now_iso()))
    conn.commit()
    if cur.rowcount == 1:
        return "new"
    row = conn.execute("SELECT outcome FROM billing_stripe_events WHERE event_id=?", (event_id,)).fetchone()
    if row and str(row["outcome"]).startswith(_DONE_PREFIXES):
        return "duplicate"
    return "retry"


def finish_event(conn: sqlite3.Connection, event_id: str, outcome: str,
                 organization_id: int | None = None) -> None:
    conn.execute("UPDATE billing_stripe_events SET outcome=?, organization_id=?, processed_at=? WHERE event_id=?",
                 (outcome, organization_id, now_iso(), event_id))
    conn.commit()


def fail_event(conn: sqlite3.Connection, event_id: str) -> None:
    """Mark a failed attempt for retry, without overwriting an outcome another
    writer already recorded for the same event."""
    conn.execute("UPDATE billing_stripe_events SET outcome='error', processed_at=? "
                 "WHERE event_id=? AND outcome IN ('processing', 'error')", (now_iso(), event_id))
    conn.commit()
