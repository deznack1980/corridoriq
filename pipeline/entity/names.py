"""Name compaction for entity resolution. Never used alone to VERIFIED-upgrade."""

from __future__ import annotations

import re

from pipeline.company_resolution.normalize import normalize_company_name
from pipeline.roc.normalize import looks_like_person_name

_INITIAL_RUN = re.compile(r"\b(?:[A-Z0-9](?:\s+[A-Z0-9]){1,4})\b")
_SPACED_LLC = re.compile(r"\bL\s+L\s+C\b")
_DISAMBIG = re.compile(r"\(\d+\)")
_THE = re.compile(r"\bTHE\b")
GENERIC_TOKENS = frozenset(
    {
        "CONSTRUCTION",
        "CONTRACTING",
        "CONTRACTOR",
        "BUILDERS",
        "BUILDER",
        "HOMES",
        "HOME",
        "SERVICES",
        "SERVICE",
        "GROUP",
        "COMPANY",
        "ENTERPRISES",
        "ENTERPRISE",
        "PROPERTIES",
        "PROPERTY",
        "LLC",
        "INC",
        "CORP",
        "CO",
    }
)


def compact_company_name(raw: str | None) -> str:
    """Stable comparison key: suffixes off, initials collapsed, (2) stripped."""
    n = normalize_company_name(raw)
    if not n:
        return ""
    n = _DISAMBIG.sub(" ", n)
    n = _SPACED_LLC.sub(" ", n)
    n = re.sub(r"\bINCCOM\b", " ", n)
    n = normalize_company_name(n)
    n = _THE.sub(" ", n)
    n = re.sub(r"\s+", " ", n).strip()
    n = _INITIAL_RUN.sub(lambda m: m.group(0).replace(" ", ""), n)
    return re.sub(r"\s+", " ", n).strip()


def name_tokens(raw: str | None) -> set[str]:
    return {t for t in compact_company_name(raw).split() if t}


def distinctive_tokens(raw: str | None) -> set[str]:
    return {t for t in name_tokens(raw) if t not in GENERIC_TOKENS and len(t) > 1}


def token_jaccard(a: str | None, b: str | None) -> float:
    sa, sb = name_tokens(a), name_tokens(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def is_person(raw: str | None) -> bool:
    return bool(raw) and looks_like_person_name(raw)
