"""First-party acquisition events.

Stored per event: name, time, a random per-visit ID generated in the browser
(not a fingerprint), user and contractor tenant when known, the acquisition
channel, referral code / supplier, UTM values, and the page path. Never stored:
IP address, user agent, email, names, passwords, or session tokens.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

# Events a browser may report. Everything else is recorded by the server.
CLIENT_EVENTS = {"landing_view", "supplier_referral_view", "signup_started", "material_request_started"}
SERVER_EVENTS = {"signup_completed", "login", "repeat_login", "contractor_onboarding_completed",
                 "material_request_created", "material_request_sent"}
EVENTS = CLIENT_EVENTS | SERVER_EVENTS

UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_content")
_UTM_VALUE = re.compile(r"^[A-Za-z0-9 ._~+-]{1,80}$")
_VISIT_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
_PAGES = {"contractor-join", "contractor-account", "contractor-home", "contractor-request", "supplier-inbox"}

QR_MEDIUMS = {"qr", "qr_code", "counter_qr"}
REP_MEDIUMS = {"sales_rep", "rep", "salesrep"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_utm(raw: dict | None) -> dict:
    """Keep only well-formed standard UTM values; drop everything else."""
    out = {}
    for key in UTM_KEYS:
        val = (raw or {}).get(key)
        if isinstance(val, str):
            val = val.strip()
            if _UTM_VALUE.fullmatch(val):
                out[key] = val.lower()
    return out


def clean_visit_id(raw) -> str | None:
    return raw if isinstance(raw, str) and _VISIT_ID.fullmatch(raw) else None


def derive_channel(utm: dict, referral_code: str | None) -> str:
    """Coarse acquisition channel: qr, sales_rep, linkedin, supplier_referral,
    campaign, or direct."""
    medium, source = utm.get("utm_medium"), utm.get("utm_source")
    if medium in QR_MEDIUMS:
        return "qr"
    if medium in REP_MEDIUMS:
        return "sales_rep"
    if source == "linkedin" or medium == "linkedin":
        return "linkedin"
    if referral_code:
        return "supplier_referral"
    if utm:
        return "campaign"
    return "direct"


def record(conn: sqlite3.Connection, event: str, *, visit_id=None, user_id=None,
           contractor_tenant_id=None, referral_code=None, supplier_tenant_id=None,
           utm: dict | None = None, channel=None, page=None, commit=True) -> None:
    if event not in EVENTS:
        raise ValueError("unknown event")
    utm = clean_utm(utm)
    page = page if page in _PAGES else None
    conn.execute(
        "INSERT INTO acquisition_events (event, created_at, visit_id, user_id, contractor_tenant_id, channel, "
        "referral_code, supplier_tenant_id, utm_source, utm_medium, utm_campaign, utm_content, page) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event, _now(), clean_visit_id(visit_id), user_id, contractor_tenant_id,
         channel or derive_channel(utm, referral_code), referral_code, supplier_tenant_id,
         utm.get("utm_source"), utm.get("utm_medium"), utm.get("utm_campaign"), utm.get("utm_content"), page))
    if commit:
        conn.commit()


def record_for_contractor(conn, event, profile, *, user_id, visit_id=None, page=None, commit=True):
    """Server-side event carrying the contractor's stored first-touch attribution."""
    record(conn, event, visit_id=visit_id, user_id=user_id, contractor_tenant_id=profile["tenant_id"],
           referral_code=profile["referral_code"], supplier_tenant_id=profile["referring_supplier_tenant_id"],
           utm={k: profile[k] for k in UTM_KEYS if profile[k]}, channel=profile["acquisition_channel"],
           page=page, commit=commit)


def funnel(conn: sqlite3.Connection) -> list[dict]:
    """Event counts by channel and referral code (owner report)."""
    rows = conn.execute(
        "SELECT event, channel, COALESCE(referral_code, '') AS referral_code, COUNT(*) AS n, "
        "COUNT(DISTINCT COALESCE(visit_id, 'u' || user_id)) AS visitors "
        "FROM acquisition_events GROUP BY 1, 2, 3 ORDER BY 2, 3, 1").fetchall()
    return [dict(r) for r in rows]
