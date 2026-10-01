"""Offline reports for one shadow run. INTERNAL / CONFIDENTIAL.

Every CSV cell is guarded against spreadsheet formula injection. Output goes
to a new folder (never overwritten)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

_FORMULA = ("=", "+", "-", "@", "\t", "\r")
BANNER = "INTERNAL / CONFIDENTIAL — CorridorIQ identity shadow (not production data)"


def safe_cell(v):
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return "'" + s if s.startswith(_FORMULA) else s


def _csv(path: Path, header, rows):
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow([BANNER])
        w.writerow(header)
        for r in rows:
            w.writerow([safe_cell(x) for x in r])


def _sig_labels(s) -> dict:
    """signature id → (family, sample original name, canonical id)."""
    out = {}
    for r in s.execute(
            "SELECT il.signature_id, si.family, si.original_name, il.canonical_id FROM identity_links il "
            "JOIN source_identities si ON si.identity_id = il.identity_id WHERE il.signature_id IS NOT NULL"):
        out.setdefault(r[0], (r[1], r[2], r[3]))
    return out


def write_reports(s, batch_id: str, summary: dict, out_dir) -> Path:
    folder = Path(out_dir).expanduser().resolve() / f"identity-shadow-{batch_id[:12]}"
    folder.mkdir(parents=True, exist_ok=False)
    names = {r[0]: r[1] for r in s.execute("SELECT canonical_id, display_name FROM canonical_companies")}
    labels = _sig_labels(s)

    review_rows = []
    for r in s.execute("SELECT * FROM resolution_reviews ORDER BY review_kind, review_id"):
        subj = r["subject"].split("|")
        first = labels.get(subj[0], ("", r["subject"], None))
        cands = []
        for sid in json.loads(r["candidate_canonicals"]):
            lab = labels.get(sid)
            if lab and lab[2]:
                cands.append(f"{lab[2]} {names.get(lab[2], '')}")
        review_rows.append((r["review_id"], r["review_kind"], first[1], first[0], "; ".join(cands[:6]),
                            r["supporting_evidence"], r["conflicting_evidence"], r["families"], r["reason"],
                            r["suggested_action"]))
    _csv(folder / "review_queue.csv",
         ["review_id", "review_kind", "source_identity", "source_family", "candidate_companies",
          "supporting_evidence", "conflicting_evidence", "source_families", "reason", "suggested_reviewer_action"],
         review_rows)

    diffs = {k: [] for k in ("WOULD_SPLIT", "WOULD_MERGE", "UNREPRESENTED")}
    for r in s.execute("SELECT * FROM shadow_diff"):
        diffs.setdefault(r["diff_kind"], []).append((r["subject"], r["detail_json"]))
    _csv(folder / "would_split.csv", ["production_company_id", "detail"], diffs["WOULD_SPLIT"])
    _csv(folder / "would_merge.csv", ["shadow_canonical_id", "detail"], diffs["WOULD_MERGE"])
    _csv(folder / "unrepresented_production.csv", ["production_company_id", "detail"], diffs["UNREPRESENTED"])
    contrib = summary["analysis"]["source_contribution"]
    cols = sorted({k for row in contrib.values() for k in row})
    _csv(folder / "source_contribution.csv", ["family", *cols],
         [(f, *[row.get(c) for c in cols]) for f, row in sorted(contrib.items())])
    (folder / "metrics.json").write_text(json.dumps({"banner": BANNER, **summary}, indent=2, default=str),
                                         encoding="utf-8")
    (folder / "summary.md").write_text(_summary_md(summary, review_rows), encoding="utf-8")
    return folder


def _md(v) -> str:
    return str(v).replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;").replace("\n", " ")


def _summary_md(summary, review_rows) -> str:
    a, d, m = summary["analysis"], summary["analysis"]["diff"], summary["analysis"]["metrics"]
    lines = [f"# Identity Evidence Shadow — run {summary['batch_id']}", "", f"> **{BANNER}**", "",
             f"Versions: parser `{summary['versions']['parser']}` · normalizer `{summary['versions']['normalizer']}` "
             f"· rules `{summary['versions']['rules']}`", "", "## Production vs shadow", "| Measure | Value |", "|---|---:|"]
    lines += [f"| {_md(k)} | {_md(v)} |" for k, v in d.items() if not isinstance(v, dict)]
    lines += ["", "Would-split reasons: " + _md(json.dumps(d.get("would_split_reasons", {}))), "",
              "## Coverage", "| Metric | Value |", "|---|---:|"]
    lines += [f"| {_md(k)} | {_md(v)} |" for k, v in sorted(m.items())]
    lines += ["", "## New evidence for production companies", "| Measure | Value |", "|---|---:|"]
    lines += [f"| {_md(k)} | {_md(v)} |" for k, v in a["new_evidence"].items()]
    lines += ["", "## Review queue", "| Kind | Cases |", "|---|---:|"]
    lines += [f"| {_md(k)} | {v} |" for k, v in sorted(a["reviews"].items())]
    lines += ["", "ROC-only entities are licensed identities with **no observed activity in current CorridorIQ "
              "permit coverage** — not evidence of real-world inactivity.", ""]
    return "\n".join(lines)
