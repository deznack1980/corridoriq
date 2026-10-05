"""billing_accounts / billing_stripe_events persistence."""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone

# Serializes Customer creation, Checkout creation and webhook application in
# this process, so a double click or two concurrent deliveries of one event
# cannot create duplicate Stripe Customers or interleave state writes.
BILLING_LOCK = threading.RLock()

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


def update_account(conn: sqlite3.Connection, organization_id: int, **fields) -> None:
    bad = set(fields) - _ACCOUNT_COLUMNS
    if bad:
        raise ValueError(f"unknown billing column(s): {sorted(bad)}")
    if not fields:
        return
    fields["updated_at"] = now_iso()
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE billing_accounts SET {cols} WHERE organization_id=?",
                 (*fields.values(), organization_id))
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
