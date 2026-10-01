"""Keyword index over CEO chunks. No vector database."""

from __future__ import annotations

import re
from pathlib import Path

from agents.ceo.retrieval.loader import Chunk, default_corpus, load_chunks

_TOKEN = re.compile(r"[a-z0-9_]{3,}")


def tokenize(text: str) -> set[str]:
    return set(_TOKEN.findall(text.lower()))


class Index:
    def __init__(self, chunks: list[Chunk]):
        self.chunks = chunks

    @classmethod
    def build(cls, paths: list[Path]) -> "Index":
        return cls(load_chunks(paths))

    def search(self, query: str, limit: int) -> list[dict]:
        needed = tokenize(query)
        if not needed:
            return []
        scored = []
        for chunk in self.chunks:
            overlap = needed & tokenize(chunk.text)
            if not overlap:
                continue
            scored.append((len(overlap), chunk))
        scored.sort(key=lambda item: (-item[0], item[1].source, item[1].title))
        results = []
        for score, chunk in scored[:limit]:
            row = chunk.as_dict()
            row["score"] = score
            results.append(row)
        return results


def build_default_index(reports_dir: Path | None = None) -> Index:
    return Index.build(default_corpus(reports_dir))
