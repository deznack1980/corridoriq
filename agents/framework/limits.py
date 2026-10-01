"""Context and model-call limits for a single CEO run."""

from __future__ import annotations

import os


class CallLimitExceeded(RuntimeError):
    """Raised when a run tries to make more model calls than allowed."""


class CallLimiter:
    def __init__(self, max_calls: int):
        if max_calls < 0:
            raise ValueError("max_calls must be >= 0")
        self.max_calls = max_calls
        self.calls = 0

    def acquire(self) -> int:
        if self.calls >= self.max_calls:
            raise CallLimitExceeded(
                f"Model call limit reached ({self.max_calls} per CEO run)."
            )
        self.calls += 1
        return self.calls


def max_model_calls() -> int:
    raw = os.environ.get("CEO_MAX_MODEL_CALLS", "2")
    try:
        return max(0, int(raw))
    except ValueError:
        return 2


def max_context_chars() -> int:
    raw = os.environ.get("CEO_MAX_CONTEXT_CHARS", "12000")
    try:
        return max(500, int(raw))
    except ValueError:
        return 12000


def retrieval_limit() -> int:
    raw = os.environ.get("CEO_RETRIEVAL_LIMIT", "8")
    try:
        return max(1, int(raw))
    except ValueError:
        return 8


def chunk_chars() -> int:
    raw = os.environ.get("CEO_CHUNK_CHARS", "1200")
    try:
        return max(200, int(raw))
    except ValueError:
        return 1200


def clip(text: str, limit: int | None = None) -> str:
    bound = max_context_chars() if limit is None else limit
    if text is None:
        return ""
    if len(text) <= bound:
        return text
    return text[:bound] + "\n[truncated by CEO context limit]"
