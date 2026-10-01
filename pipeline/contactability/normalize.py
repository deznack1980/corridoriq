"""Contact value normalization and confidence labels."""

from __future__ import annotations

import re

from pipeline.company_resolution.normalize import normalize_phone

VERIFIED = "VERIFIED"
CANDIDATE = "CANDIDATE"
INFERRED_UNVERIFIED = "INFERRED_UNVERIFIED"
STALE = "STALE"
CONFLICT = "CONFLICT"
REJECTED = "REJECTED"

STATUS_RANK = {
    REJECTED: 0,
    INFERRED_UNVERIFIED: 1,
    STALE: 1,
    CANDIDATE: 2,
    CONFLICT: 2,
    VERIFIED: 3,
}

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(value: str | None) -> str | None:
    if not value or "@" not in str(value):
        return None
    email = str(value).strip().lower()
    if not _EMAIL_RE.match(email):
        return None
    return email


def normalize_website(value: str | None) -> str | None:
    if not value:
        return None
    text = str(value).strip()
    if not text or " " in text:
        return None
    text = text.rstrip("/")
    if text.startswith("http://"):
        text = "https://" + text[len("http://") :]
    if not text.startswith("https://"):
        text = "https://" + text
    host = text.split("://", 1)[1].split("/")[0].lower()
    if "." not in host or host.startswith("."):
        return None
    return text.lower()


def normalize_contact_value(contact_type: str, value: str | None) -> str | None:
    raw = None if value is None else str(value).strip()
    if not raw:
        return None
    if contact_type in {"website", "contact_form"}:
        return normalize_website(raw)
    if "@" in raw:
        return normalize_email(raw)
    if contact_type in {
        "business_phone",
        "office",
        "sales_contact",
        "owner",
        "manager",
        "estimator",
        "purchasing",
        "operations",
    }:
        phone = normalize_phone(raw)
        return phone or raw
    return raw


def research_status_to_verification(status: str | None, confidence: str | None) -> str:
    st = (status or "").strip()
    conf = (confidence or "").strip().lower()
    if st == "Found" and conf in {"high", "verified", ""}:
        return VERIFIED
    if st == "Found":
        return VERIFIED
    if st in {"Partial", "Ambiguous"}:
        return CANDIDATE
    return CANDIDATE
