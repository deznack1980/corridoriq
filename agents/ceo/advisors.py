"""Future specialist advisors. Interfaces only. Not built in v0.1."""

from __future__ import annotations

FUTURE_ADVISORS = (
    "sales",
    "data_intelligence",
    "product",
    "finance",
    "customer_success",
)


def advise(name: str, _request: dict | None = None) -> dict:
    if name not in FUTURE_ADVISORS:
        raise KeyError(f"Unknown advisor: {name}")
    raise NotImplementedError(
        f"The {name} advisor is a future module. "
        "The CEO remains the only synthesis layer in v0.1."
    )
