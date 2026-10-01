"""Customer-safe contact view over company_contact_channels.

There is one contact system: company_contact_channels, written by the
contactability pipeline. This module only reads it. The channel the portal
shows as callable or emailable is picked by gates.contact_state, the same
function the trust gates use, so the displayed contact and the CALL_NOW /
EMAIL decision cannot disagree.

Rules:
- Only rows on this exact company_id are read. A duplicate or similarly named
  company's contacts are never shown here.
- Rows copied from a linked (canonical peer) record are labelled inherited and
  are never actionable.
- CANDIDATE and inferred rows are never shown as verified.
- Nothing is inferred or composed. A missing value stays missing.
"""

from __future__ import annotations

import sqlite3

from pipeline.trust import gates as G

NOT_VERIFIED_TEXT = "Contact not yet verified."

VERIFIED = "VERIFIED"
INHERITED = "INHERITED_UNVERIFIED"
NOT_VERIFIED = "NOT_VERIFIED"

# Public wording for where a verified value came from. Unknown families get a
# neutral label; internal family codes are never returned.
_SOURCE_TEXT = {
    "prior_research": "CorridorIQ research",
    "official_website": "Company website",
    "roc": "State contractor license record",
    "public_directory": "Public business directory",
    "bbb": "Public business directory",
    "public_business_record": "Public business record",
    "public_licensing": "State license record",
    "canonical_peer": "Linked company record",
}
_PHONE_TYPES = {"business_phone"}
_EMAIL_TYPES = {"business_email", "estimator", "purchasing", "office"}


def _rows(conn: sqlite3.Connection, company_id: int) -> list[dict]:
    try:
        return [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, company_id, contact_type, contact_value, normalized_value,
                       verification_status, source_family, public_business_contact,
                       contact_name, title, decision_maker_class, verified_at, is_primary
                FROM company_contact_channels
                WHERE company_id = ? AND status = 'active'
                ORDER BY is_primary DESC, id
                """,
                (int(company_id),),
            )
        ]
    except sqlite3.Error:
        return []


def _source(row: dict) -> str:
    return _SOURCE_TEXT.get(row.get("source_family") or "", "Public source")


def _is_phone(row: dict) -> bool:
    return row["contact_type"] in _PHONE_TYPES or (
        row["contact_type"] == "office" and str(row.get("normalized_value") or "").isdigit()
    )


def _is_email(row: dict) -> bool:
    return row["contact_type"] in _EMAIL_TYPES and "@" in str(row.get("contact_value") or "")


def _item(row: dict | None) -> dict | None:
    if row is None:
        return None
    return {
        "value": row["contact_value"],
        "verified_at": row.get("verified_at"),
        "source": _source(row),
    }


def verified_contact_view(conn: sqlite3.Connection, company_id: int) -> dict:
    rows = _rows(conn, company_id)
    public = [r for r in rows if int(r.get("public_business_contact") or 0)]
    own = [r for r in public if r["verification_status"] == "VERIFIED" and r["source_family"] != "canonical_peer"]
    peer = [r for r in public if r["source_family"] == "canonical_peer"]

    gate = G.contact_state({"contacts": rows})
    picked = next((r for r in rows if r["id"] == gate.get("channel_id")), None)

    # Values shown as verified: this company's own VERIFIED rows only.
    phone = picked if gate["state"] == G.VERIFIED_PHONE else next((r for r in own if _is_phone(r)), None)
    email = picked if gate["state"] == G.VERIFIED_EMAIL else next((r for r in own if _is_email(r)), None)
    address = next((r for r in own if r["contact_type"] == "business_address"), None)
    website = next((r for r in own if r["contact_type"] == "website"), None)
    named = next(
        (r for r in own if r.get("contact_name") and r["contact_type"] != "qualifying_party"),
        None,
    )
    shown = [r for r in (phone, email, address, website, named) if r is not None]
    dates = sorted({r["verified_at"] for r in shown if r.get("verified_at")})

    actionable = gate["state"] in {G.VERIFIED_PHONE, G.VERIFIED_EMAIL}
    if actionable:
        status, label = VERIFIED, "Verified on this company's record"
    elif gate["state"] == G.PEER_ONLY:
        status, label = INHERITED, "Inherited from a linked company record; not verified for this company"
    else:
        status, label = NOT_VERIFIED, NOT_VERIFIED_TEXT

    inherited = [
        {
            "type": "phone" if _is_phone(r) else "email" if _is_email(r) else r["contact_type"].replace("_", " "),
            "value": r["contact_value"],
            "name": r.get("contact_name"),
            "note": "From a linked company record. Not verified for this company. Not used for recommendations.",
        }
        for r in peer
        if r["verification_status"] in {"VERIFIED", "CANDIDATE", "INFERRED_UNVERIFIED"}
        and (_is_phone(r) or _is_email(r))
    ]
    return {
        "status": status,
        "status_label": label,
        "actionable": actionable,
        "recommended_channel": "phone" if gate["state"] == G.VERIFIED_PHONE else "email" if gate["state"] == G.VERIFIED_EMAIL else None,
        "contact_name": None if named is None else named["contact_name"],
        "title": None if named is None else (named.get("title") or None),
        "phone": _item(phone),
        "email": _item(email),
        "address": _item(address),
        "website": _item(website),
        "last_verified": dates[-1] if dates else None,
        "inherited": inherited,
    }
