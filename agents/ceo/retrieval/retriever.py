"""Public retrieval entry point with a hard result cap."""

from __future__ import annotations

from pathlib import Path

from agents.ceo.retrieval.index import Index, build_default_index
from agents.framework.limits import retrieval_limit

_CACHE: dict[tuple, Index] = {}


def retrieve(
    query: str,
    *,
    reports_dir: Path | None = None,
    extra_paths: list[Path] | None = None,
    limit: int | None = None,
) -> list[dict]:
    cap = retrieval_limit() if limit is None else limit
    if extra_paths is not None:
        index = Index.build(list(extra_paths))
    else:
        key = (str(reports_dir) if reports_dir else "",)
        index = _CACHE.get(key)
        if index is None:
            index = build_default_index(reports_dir)
            _CACHE[key] = index
    return index.search(query, cap)


def clear_cache() -> None:
    _CACHE.clear()
