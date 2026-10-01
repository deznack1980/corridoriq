"""Load charter, knowledge, and selected reports into inspectable chunks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from agents.ceo.paths import CHARTER_DIR, KNOWLEDGE_DIR, REPO_ROOT
from agents.framework.limits import chunk_chars

_PHONE = re.compile(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}")
_EMAIL = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_HEADING = re.compile(r"(?m)^##\s+(.+)$")

REPORT_PREFIXES = (
    "sales_trial_",
    "sales_lanes_",
    "sales_readiness_",
    "entity_contactability_",
    "roc_identity_",
    "account_priority_",
    "customer_relevance_",
    "contractor_intel_",
)


@dataclass(frozen=True)
class Chunk:
    text: str
    source: str
    timestamp: str | None
    category: str
    confidence: str
    title: str

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "source": self.source,
            "timestamp": self.timestamp,
            "category": self.category,
            "confidence": self.confidence,
            "title": self.title,
        }


def scrub_contacts(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _PHONE.sub("[phone]", text)
    return text


def categorize(path: Path) -> tuple[str, str]:
    """Return (retrieval category, confidence)."""
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    if "charter" in parts:
        return "charter", "HIGH"
    if "knowledge" in parts:
        return "business_knowledge", "HIGH"
    if name.startswith("sales_trial"):
        return "sales_trial", "HIGH"
    if name.startswith("sales_lanes") or name.startswith("sales_readiness"):
        return "sales_lanes", "HIGH"
    if name.startswith("roc_identity"):
        return "roc", "HIGH"
    if name.startswith("account_priority"):
        return "account_priority", "HIGH"
    if name.startswith("customer_relevance"):
        return "relevance", "HIGH"
    if name.startswith("entity_contactability"):
        return "contactability", "HIGH"
    if name.startswith("contractor_intel"):
        return "contractor_intel", "HIGH"
    if "architecture" in parts or name.startswith("readme"):
        return "design_doc", "MEDIUM"
    return "document", "MEDIUM"


def _stamp_from_name(name: str) -> str | None:
    match = re.search(r"_(\d{8}T\d{6}Z)", name)
    if not match:
        match = re.search(r"_(\d{4}-\d{2}-\d{2})", name)
        return match.group(1) if match else None
    raw = match.group(1)
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}T{raw[9:11]}:{raw[11:13]}:{raw[13:15]}Z"


def chunk_markdown(path: Path, *, scrub: bool) -> list[Chunk]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if scrub:
        raw = scrub_contacts(raw)
    category, confidence = categorize(path)
    timestamp = _stamp_from_name(path.name)
    source = path.name if scrub else str(path)
    matches = list(_HEADING.finditer(raw))
    blocks = []
    if not matches:
        blocks.append((path.stem, raw))
    else:
        if matches[0].start() > 0 and raw[: matches[0].start()].strip():
            blocks.append(("Overview", raw[: matches[0].start()]))
        for index, match in enumerate(matches):
            start = match.end()
            end = matches[index + 1].start() if index + 1 < len(matches) else len(raw)
            blocks.append((match.group(1).strip(), raw[start:end]))
    limit = chunk_chars()
    chunks = []
    for title, body in blocks:
        text = f"{title}\n{body}".strip()
        if not text:
            continue
        if len(text) > limit:
            text = text[:limit]
        chunks.append(
            Chunk(
                text=text,
                source=source,
                timestamp=timestamp,
                category=category,
                confidence=confidence,
                title=title[:120],
            )
        )
    return chunks


def _newest_reports(reports_dir: Path | None, keep: int = 1) -> list[Path]:
    if reports_dir is None or not reports_dir.exists():
        return []
    chosen = []
    for prefix in REPORT_PREFIXES:
        matches = sorted(
            list(reports_dir.glob(prefix + "*.md"))
            + list(reports_dir.glob(prefix + "*.json"))
        )
        chosen.extend(matches[-keep:])
    return chosen


def default_corpus(reports_dir: Path | None = None) -> list[Path]:
    files = []
    for directory in (CHARTER_DIR, KNOWLEDGE_DIR):
        if directory.exists():
            files.extend(sorted(directory.glob("*.md")))
    readme = REPO_ROOT / "README.md"
    if readme.exists():
        files.append(readme)
    architecture = REPO_ROOT / "docs" / "architecture"
    if architecture.exists():
        files.extend(sorted(architecture.glob("*.md")))
    summary = REPO_ROOT / "docs" / "data-platform" / "EXECUTIVE_SUMMARY.md"
    if summary.exists():
        files.append(summary)
    files.extend(_newest_reports(reports_dir))
    return files


def load_chunks(paths: list[Path], *, scrub_reports: bool = True) -> list[Chunk]:
    chunks = []
    for path in paths:
        scrub = scrub_reports and path.suffix.lower() in {".md", ".json"} and any(
            path.name.startswith(prefix) for prefix in REPORT_PREFIXES
        )
        chunks.extend(chunk_markdown(path, scrub=scrub))
    return chunks
