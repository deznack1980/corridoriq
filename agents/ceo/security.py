"""Redaction and the customer-output refusal."""

from __future__ import annotations

import re

_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|password|secret|token|access[_-]?key)\b\s*[:=]\s*\S+"
    ),
)


def redact(text: str) -> str:
    if not text:
        return ""
    cleaned = text
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("[REDACTED]", cleaned)
    return cleaned


def customer_safe_output(*_args, **_kwargs):
    """v0.1 has no customer-facing generator."""
    raise PermissionError(
        "Customer-facing generation is not authorized in CEO agent v0.2."
    )
