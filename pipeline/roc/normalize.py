"""ROC field normalization. Conservative; no ranking side effects."""

from __future__ import annotations

import re

from pipeline.company_resolution.normalize import (
    looks_like_company,
    normalize_city,
    normalize_company_name,
    normalize_license,
    normalize_phone,
)

_CLASS_RE = re.compile(r"([A-Z]{0,2}-?\d+[A-Z]{0,2}|[AB]|KA|KB)", re.I)
_PERSON_TOKEN = re.compile(r"^[A-Z][A-Z'\-]*$")
_TRADE_BLOCK = re.compile(
    r"\b(PROPANE|GAS|PLUMB|PIPE|HVAC|FIRE|BACKFLOW|PERMIT|SERVICE|SERVICES|"
    r"SYSTEMS?|ENVIRONMENT|ENVIRONMENTS|POOLS?|HOMES?|CONSTRUCTION|CONTRACT|AMERIGAS|FERRELL|"
    r"METRO|RIDER|LLC|INC|CORP|COMPANY|CO)\b"
)
_QP_SKIP = frozenset({"", "QP EXEMPT", "EXEMPT", "N/A", "NA", "NONE", "UNKNOWN"})
_PLACEHOLDER_LICENSES = frozenset({"HTE0001", "TBD", "NA", "NONE", "UNKNOWN"})


def normalize_roc_license(value: str | None) -> str | None:
    """Six-digit Arizona ROC license, or None for placeholders."""
    alnum = normalize_license(value)
    if not alnum or alnum in _PLACEHOLDER_LICENSES:
        return None
    digits = re.sub(r"\D", "", alnum)
    if digits.isdigit() and 1 <= len(digits) <= 6:
        return digits.zfill(6)
    return None


def normalize_class_code(raw_class: str | None, class_detail: str | None = None) -> str:
    text = f"{raw_class or ''} {class_detail or ''}".strip().upper()
    if not text:
        return ""
    match = re.match(r"^\s*([A-Z]{0,2}-?\d+[A-Z]{0,2}|[AB]|KA(?:-\d+)?|KB(?:-\d+)?)", text)
    if not match:
        return re.sub(r"\s+", " ", text)
    code = match.group(1).replace(" ", "")
    if re.fullmatch(r"[A-Z]{1,2}\d+[A-Z]{0,2}", code):
        prefix = re.match(r"[A-Z]+", code).group(0)
        rest = code[len(prefix):]
        code = f"{prefix}-{rest}"
    return code


def normalize_status(value: str | None) -> str:
    text = (value or "").strip().upper()
    if text in {"ACTIVE", "CURRENT"}:
        return "active"
    if text in {"INACTIVE", "EXPIRED", "SUSPENDED", "REVOKED", "CANCELLED", "CANCELED"}:
        return text.lower()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return "unknown"
    return "unknown" if text else "unknown"


def normalize_person_name(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"[.]", "", str(value).strip().upper())
    text = re.sub(r"\s+", " ", text)
    if text in _QP_SKIP:
        return ""
    if "," in text:
        last, first = [p.strip() for p in text.split(",", 1)]
        text = f"{first} {last}".strip()
    return text


def looks_like_person_name(value: str | None) -> bool:
    if not value or looks_like_company(value):
        return False
    n = normalize_company_name(value)
    if not n or _TRADE_BLOCK.search(n):
        return False
    tokens = [t for t in n.split() if t]
    if len(tokens) < 2 or len(tokens) > 4:
        return False
    return all(_PERSON_TOKEN.match(t) for t in tokens)


def usable_phone(value: str | None) -> str | None:
    return normalize_phone(value)


def usable_email(value: str | None) -> str | None:
    if not value or "@" not in str(value):
        return None
    email = str(value).strip().lower()
    return email if "." in email.split("@", 1)[-1] else None


def usable_address(row: dict) -> bool:
    return bool((row.get("address_line_1") or "").strip() and (row.get("city") or "").strip())


__all__ = [
    "looks_like_person_name",
    "normalize_city",
    "normalize_class_code",
    "normalize_company_name",
    "normalize_person_name",
    "normalize_roc_license",
    "normalize_status",
    "usable_address",
    "usable_email",
    "usable_phone",
]
