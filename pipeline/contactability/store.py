"""Idempotent contact-channel writes. Never touch CRM or company profile phones."""

from __future__ import annotations

import sqlite3

from pipeline.config.settings import CONTACT_MODEL_VERSION
from pipeline.contactability.normalize import STATUS_RANK, normalize_contact_value
from pipeline.contactability.roles import UNKNOWN, classify_decision_maker
from pipeline.db.database import now_iso

CONTACT_TYPES = (
    "business_phone",
    "business_email",
    "website",
    "contact_form",
    "sales_contact",
    "owner",
    "manager",
    "estimator",
    "purchasing",
    "operations",
    "office",
    "qualifying_party",
    "business_address",
    "named_contact",
)


def upsert_channel(
    conn: sqlite3.Connection,
    *,
    company_id: int,
    contact_type: str,
    contact_value: str,
    source_family: str,
    source_reference: str | None = None,
    contact_name: str | None = None,
    title: str | None = None,
    verification_status: str,
    confidence: float | None = None,
    public_business_contact: bool = True,
    notes: str | None = None,
    canonical_company_id: int | None = None,
    verified_at: str | None = None,
    is_primary: bool = False,
) -> bool:
    """Insert or upgrade a channel. Returns True if a row was written/updated."""
    normalized = normalize_contact_value(contact_type, contact_value)
    if not normalized:
        return False
    now = now_iso()
    existing = conn.execute(
        """
        SELECT id, verification_status, discovered_at FROM company_contact_channels
        WHERE company_id=? AND contact_type=? AND normalized_value=? AND source_family=?
        """,
        (company_id, contact_type, normalized, source_family),
    ).fetchone()
    dm = classify_decision_maker(title)
    if existing:
        old_rank = STATUS_RANK.get(existing["verification_status"], 0)
        new_rank = STATUS_RANK.get(verification_status, 0)
        if new_rank < old_rank:
            return False
        conn.execute(
            """
            UPDATE company_contact_channels SET
                contact_value=?, contact_name=COALESCE(?, contact_name),
                title=COALESCE(?, title), original_title=COALESCE(original_title, ?),
                decision_maker_class=?, source_reference=COALESCE(?, source_reference),
                verified_at=COALESCE(?, verified_at), confidence=?,
                verification_status=?, is_primary=MAX(is_primary, ?),
                public_business_contact=?, notes=COALESCE(?, notes),
                canonical_company_id=COALESCE(canonical_company_id, ?),
                updated_at=?
            WHERE id=?
            """,
            (
                str(contact_value).strip(),
                contact_name,
                title,
                title,
                dm if title else UNKNOWN,
                source_reference,
                verified_at,
                confidence,
                verification_status,
                1 if is_primary else 0,
                1 if public_business_contact else 0,
                notes,
                canonical_company_id,
                now,
                int(existing["id"]),
            ),
        )
        return True
    conn.execute(
        """
        INSERT INTO company_contact_channels (
            company_id, canonical_company_id, contact_type, contact_value,
            normalized_value, contact_name, title, original_title,
            decision_maker_class, source_family, source_reference, discovered_at,
            verified_at, confidence, verification_status, status, is_primary,
            public_business_contact, notes, model_version, created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',?,?,?,?,?,?)
        """,
        (
            company_id,
            canonical_company_id,
            contact_type,
            str(contact_value).strip(),
            normalized,
            contact_name,
            title,
            title,
            dm if title else UNKNOWN,
            source_family,
            source_reference,
            now,
            verified_at if verification_status == "VERIFIED" else verified_at,
            confidence,
            verification_status,
            1 if is_primary else 0,
            1 if public_business_contact else 0,
            notes,
            CONTACT_MODEL_VERSION,
            now,
            now,
        ),
    )
    return True


def mark_primaries(conn: sqlite3.Connection, company_id: int) -> None:
    """Pick one primary phone and one primary email. Prefer verified public business."""
    now = now_iso()
    for ctype in ("business_phone", "business_email", "website"):
        conn.execute(
            "UPDATE company_contact_channels SET is_primary=0, updated_at=? "
            "WHERE company_id=? AND contact_type=?",
            (now, company_id, ctype),
        )
        row = conn.execute(
            """
            SELECT id FROM company_contact_channels
            WHERE company_id=? AND contact_type=? AND status='active'
              AND verification_status IN ('VERIFIED','CANDIDATE')
            ORDER BY CASE verification_status WHEN 'VERIFIED' THEN 0 ELSE 1 END,
                     public_business_contact DESC, id
            LIMIT 1
            """,
            (company_id, ctype),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE company_contact_channels SET is_primary=1, updated_at=? WHERE id=?",
                (now, int(row["id"])),
            )


def share_channels_across_canonical(conn: sqlite3.Connection) -> dict:
    """Copy usable channels to sibling raw rows. Provenance records the peer id."""
    written = 0
    groups = conn.execute(
        """
        SELECT canonical_company_id, GROUP_CONCAT(raw_company_id) AS ids
        FROM company_entity_links
        GROUP BY canonical_company_id
        """
    ).fetchall()
    for group in groups:
        members = [int(x) for x in str(group["ids"]).split(",") if x]
        if len(members) < 2:
            continue
        channels = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT * FROM company_contact_channels
                WHERE company_id IN ({",".join("?" * len(members))})
                  AND status='active'
                  AND contact_type IN ('business_phone','business_email','website',
                       'contact_form','estimator','purchasing','office','named_contact')
                """,
                members,
            )
        ]
        for dest in members:
            for ch in channels:
                if int(ch["company_id"]) == dest:
                    continue
                if upsert_channel(
                    conn,
                    company_id=dest,
                    canonical_company_id=int(group["canonical_company_id"]),
                    contact_type=ch["contact_type"],
                    contact_value=ch["contact_value"],
                    source_family="canonical_peer",
                    source_reference=f"peer:{ch['company_id']}:{ch['source_family']}",
                    contact_name=ch["contact_name"],
                    title=ch["original_title"] or ch["title"],
                    verification_status=ch["verification_status"],
                    confidence=ch["confidence"],
                    public_business_contact=bool(ch["public_business_contact"]),
                    notes=f"Shared from raw company {ch['company_id']}; original source preserved.",
                    verified_at=ch["verified_at"],
                ):
                    written += 1
            mark_primaries(conn, dest)
    conn.commit()
    return {"channels_shared": written}


def canonical_id_for(conn: sqlite3.Connection, company_id: int) -> int | None:
    row = conn.execute(
        "SELECT canonical_company_id FROM company_entity_links WHERE raw_company_id=? LIMIT 1",
        (company_id,),
    ).fetchone()
    return None if row is None else int(row["canonical_company_id"])
