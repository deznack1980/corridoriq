"""Tenant-scoped report generation for one match run.

A report is produced from exactly one TenantStore and one run ID; there is no
API that accepts several tenants, so a report cannot mix suppliers' books.
Output goes to a new folder ``<out>/<tenant_id>-<run_id[:12]>/`` (never
overwritten). Every CSV cell is guarded against spreadsheet formula injection
and Markdown output escapes table/HTML characters.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from pipeline.book_match.matcher import HIGH, REVIEW, UNMATCHED, VERIFIED, explain

_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def escape_cell(value):
    """Neutralize spreadsheet formulas: prefix risky text with an apostrophe."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return value
    s = str(value)
    return "'" + s if s.startswith(_FORMULA_PREFIX) else s


def unescape_cell(value: str) -> str:
    if value and len(value) > 1 and value[0] == "'" and value[1] in "=+-@\t\r":
        return value[1:]
    return value


def _md(value) -> str:
    s = "" if value is None else str(value)
    return (s.replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")
            .replace("\n", " ").replace("`", "'"))


def _write_csv(path: Path, header: list[str], rows) -> int:
    n = 0
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        w.writerow(header)
        for r in rows:
            w.writerow([escape_cell(v) for v in r])
            n += 1
    return n


def generate_reports(store, run_id: str, out_dir, index=None) -> Path:
    tid = store.tenant_id
    out_root = Path(out_dir).expanduser().resolve()
    with store.connect() as conn:
        run = conn.execute("SELECT * FROM match_runs WHERE run_id=? AND tenant_id=?", (run_id, tid)).fetchone()
        if run is None:
            raise LookupError("match run not found for this tenant")
        batch = conn.execute("SELECT * FROM import_batches WHERE batch_id=? AND tenant_id=?",
                             (run["batch_id"], tid)).fetchone()
        accounts = {r["supplier_account_id"]: dict(r) for r in conn.execute(
            "SELECT * FROM book_accounts WHERE tenant_id=?", (tid,))}
        matches = [dict(r) for r in conn.execute(
            "SELECT * FROM account_matches WHERE run_id=? AND tenant_id=? ORDER BY supplier_account_id",
            (run_id, tid))]
        classes = [dict(r) for r in conn.execute(
            "SELECT * FROM account_classifications WHERE run_id=? AND tenant_id=? ORDER BY supplier_account_id",
            (run_id, tid))]
        net_new = [dict(r) for r in conn.execute(
            "SELECT * FROM net_new_candidates WHERE run_id=? AND tenant_id=? "
            "ORDER BY recent_permits DESC, display_name", (run_id, tid))]
        issues = [dict(r) for r in conn.execute(
            "SELECT row_number, level, reason, field_preview FROM import_issues WHERE batch_id=? AND tenant_id=? "
            "ORDER BY row_number", (run["batch_id"], tid))]

    folder = out_root / f"{tid}-{run_id[:12]}"
    folder.mkdir(parents=True, exist_ok=False)

    def cname(cid):
        f = index.get(cid) if (index is not None and cid is not None) else None
        return (f.display_name, f.city) if f else ("", "")

    _write_csv(folder / "matches.csv",
               ["supplier_account_id", "account_name", "confidence", "company_id", "company_name",
                "matching_evidence", "conflicting_evidence", "review_reason"],
               ((m["supplier_account_id"], accounts.get(m["supplier_account_id"], {}).get("company_name"),
                 m["confidence"], m["company_id"], cname(m["company_id"])[0],
                 explain(json.loads(m["evidence_json"])), explain(json.loads(m["conflicts_json"])),
                 m["review_reason"]) for m in matches))

    review_rows = []
    for m in matches:
        if m["confidence"] != REVIEW:
            continue
        a = accounts.get(m["supplier_account_id"], {})
        cands = json.loads(m["candidates_json"] or "[]")
        others = "; ".join(f"{c['company_id']} {cname(c['company_id'])[0]} ({explain(c['evidence'])})"
                           for c in cands[1:])
        name, city = cname(m["company_id"])
        review_rows.append((run_id, m["supplier_account_id"], a.get("company_name"), a.get("dba_name"),
                            a.get("phone"), a.get("address"), a.get("city"), a.get("postal_code"),
                            m["company_id"], name, city, explain(json.loads(m["evidence_json"])),
                            explain(json.loads(m["conflicts_json"])), others, m["review_reason"],
                            "", "", "", ""))
    _write_csv(folder / "review.csv",
               ["run_id", "supplier_account_id", "account_name", "account_dba", "account_phone", "account_address",
                "account_city", "account_postal_code", "candidate_company_id", "candidate_company_name",
                "candidate_city", "matching_evidence", "conflicting_evidence", "other_candidates",
                "review_reason", "decision", "decided_company_id", "reviewer", "note"], review_rows)

    _write_csv(folder / "classification.csv",
               ["supplier_account_id", "account_name", "branch", "assigned_rep", "last_purchase_date",
                "classification", "purchase_status", "market_status", "market_trend", "recent_permits",
                "prior_permits", "last_permit_date", "wet_side_relevant", "match_basis", "company_id",
                "company_name"],
               ((c["supplier_account_id"], accounts.get(c["supplier_account_id"], {}).get("company_name"),
                 accounts.get(c["supplier_account_id"], {}).get("branch"),
                 accounts.get(c["supplier_account_id"], {}).get("assigned_rep"),
                 accounts.get(c["supplier_account_id"], {}).get("last_purchase_on"),
                 c["classification"], c["purchase_status"], c["market_status"], c["market_trend"],
                 c["recent_permits"], c["prior_permits"], c["last_permit_date"],
                 "yes" if c["wet_relevant"] else "no", c["match_basis"], c["company_id"],
                 cname(c["company_id"])[0]) for c in classes))

    _write_csv(folder / "net_new.csv",
               ["company_id", "company_name", "city", "wet_side_lanes", "recent_permits", "last_permit_date",
                "possible_existing_account_under_review"],
               ((n["company_id"], n["display_name"], n["city"], n["lanes"], n["recent_permits"],
                 n["last_permit_date"], n["possible_account"]) for n in net_new))

    _write_csv(folder / "import_issues.csv", ["row_number", "level", "reason", "field_preview"],
               ((i["row_number"], i["level"], i["reason"], i["field_preview"]) for i in issues))

    (folder / "summary.md").write_text(
        _summary(store, run, batch, matches, classes, net_new, issues, accounts, cname), encoding="utf-8")
    return folder


def _summary(store, run, batch, matches, classes, net_new, issues, accounts, cname) -> str:
    counts = {k: 0 for k in (VERIFIED, HIGH, REVIEW, UNMATCHED)}
    for m in matches:
        counts[m["confidence"]] += 1
    by_class: dict[str, int] = {}
    for c in classes:
        by_class[c["classification"]] = by_class.get(c["classification"], 0) + 1
    cfg = json.loads(run["config_json"])
    total = len(matches) or 1
    lines = [
        f"# Customer Book × Market — {_md(store.tenant.display_name)}",
        "",
        f"Tenant `{store.tenant_id}` · run `{run['run_id']}` · generated "
        f"{datetime.now(timezone.utc).isoformat(timespec='minutes')}",
        "",
        "> Confidential to this supplier. Contains only this supplier's book. "
        "CorridorIQ company data is shared public-record intelligence.",
        "",
        "## Import provenance",
        f"- Source file: `{_md(batch['source_filename'])}` (SHA-256 `{batch['source_sha256'][:16]}…`)",
        f"- Imported: {batch['imported_at']} · rows {batch['row_count']} · accepted {batch['accepted_count']} · "
        f"rejected {batch['rejected_count']} · warnings {batch['warning_count']}",
        f"- Rules: {run['rules_version']} · intelligence snapshot: `{_md(run['intel_fingerprint'])}`",
        "",
        "## Identity matching",
        "| Confidence | Accounts | Share |",
        "|---|---:|---:|",
        *[f"| {k} | {v} | {100 * v / total:.0f}% |" for k, v in counts.items()],
        "",
        "Only VERIFIED, HIGH_CONFIDENCE and human-confirmed matches are classified. "
        "REVIEW_REQUIRED accounts are listed in `review.csv` for a person to decide.",
        "",
        "## Book × Market",
        "| Classification | Accounts |",
        "|---|---:|",
        *[f"| {k} | {v} |" for k, v in sorted(by_class.items())],
        "",
    ]
    hot = [c for c in classes if c["classification"] in
           ("DORMANT_CUSTOMER_ACTIVE_MARKET", "FORMER_CUSTOMER_ACTIVE_MARKET")]
    hot.sort(key=lambda c: -(c["recent_permits"] or 0))
    if hot:
        lines += ["### Dormant or former customers with active construction (top 10)",
                  "| Account | Name | Last purchase | Recent permits | Trend |", "|---|---|---|---:|---|"]
        for c in hot[:10]:
            a = accounts.get(c["supplier_account_id"], {})
            lines.append(f"| {_md(c['supplier_account_id'])} | {_md(a.get('company_name'))} | "
                         f"{_md(a.get('last_purchase_on') or '—')} | {c['recent_permits']} | {c['market_trend']} |")
        lines.append("")
    lines += [f"## Net-new relevant contractors: {len(net_new)}",
              "Wet-side-relevant CorridorIQ companies with recent permit activity that do not confidently "
              "match an account in this book. **Geography/branch proximity is not applied yet (future work).**",
              ""]
    if net_new:
        lines += ["| Company | City | Lanes | Recent permits | Possible account under review |",
                  "|---|---|---|---:|---|"]
        for n in net_new[:10]:
            lines.append(f"| {_md(n['display_name'])} | {_md(n['city'])} | {_md(n['lanes'])} | "
                         f"{n['recent_permits']} | {_md(n['possible_account'] or '')} |")
        lines.append("")
    rejected = sum(1 for i in issues if i["level"] == "REJECTED")
    lines += [
        "## Thresholds used",
        f"- Active customer: last purchase within {cfg['active_purchase_days']} days",
        f"- Former customer: "
        + (f"no purchase for more than {cfg['former_after_days']} days (supplier policy)"
           if cfg.get("former_after_days") else "not inferred (no supplier policy set)"),
        f"- Active market: at least {cfg['market_min_permits']} permit(s) in the last {cfg['market_window_days']} days",
        f"- Evaluation date: {cfg.get('as_of') or 'run date'}",
        "",
        "## Known limitations",
        "- Permit activity covers the jurisdictions and contractor attribution CorridorIQ has; "
        "coverage varies by jurisdiction and source.",
        "- Most CorridorIQ companies have no verified phone on file, so many correct matches still need review.",
        "- Branch geography is not used yet.",
        f"- Import issues: {rejected} rejected row(s), {len(issues) - rejected} warning(s) — see `import_issues.csv`.",
        "",
    ]
    return "\n".join(lines)
