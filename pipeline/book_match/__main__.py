"""Offline Customer-Book Match CLI.

    python -m pipeline.book_match tenant-create --tenant acme-supply --name "Acme Supply"
    python -m pipeline.book_match <accounts.csv> --tenant acme-supply --out <dir>
    python -m pipeline.book_match review-apply <review.csv> --tenant acme-supply
    python -m pipeline.book_match tenant-list

Tenant root: --tenant-root, else CORRIDORIQ_TENANT_ROOT, else <DATA_DIR>/tenants.
The shared intelligence database (--intel-db, default settings.DB_PATH) is
opened read-only. Output: a new report folder under --out.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from pipeline.book_match.config import BookMatchConfig
from pipeline.tenancy import TenantError, TenantRegistry


def _tenant_root(arg):
    if arg:
        return Path(arg)
    env = os.environ.get("CORRIDORIQ_TENANT_ROOT")
    if env:
        return Path(env)
    from pipeline.config import settings
    return settings.DATA_DIR / "tenants"


def _intel_db(arg):
    if arg:
        return Path(arg)
    from pipeline.config import settings
    return settings.DB_PATH


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    commands = {"run", "tenant-create", "tenant-list", "review-apply"}
    if argv and argv[0] not in commands and not argv[0].startswith("-"):
        argv.insert(0, "run")  # spec form: python -m pipeline.book_match <accounts.csv> ...
    p = argparse.ArgumentParser(prog="python -m pipeline.book_match")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="import a book CSV, match, classify, and write reports")
    r.add_argument("csv")
    r.add_argument("--tenant", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--tenant-root")
    r.add_argument("--intel-db")
    r.add_argument("--config", help="JSON file with BookMatchConfig thresholds")

    c = sub.add_parser("tenant-create", help="register a supplier tenant")
    c.add_argument("--tenant", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--tenant-root")

    lst = sub.add_parser("tenant-list", help="list tenants (IDs, names, status only)")
    lst.add_argument("--tenant-root")

    rv = sub.add_parser("review-apply", help="store decisions from a completed review.csv")
    rv.add_argument("csv")
    rv.add_argument("--tenant", required=True)
    rv.add_argument("--tenant-root")
    rv.add_argument("--intel-db")
    rv.add_argument("--reviewer", default="unspecified")

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        registry = TenantRegistry(_tenant_root(getattr(a, "tenant_root", None)))
        if a.cmd == "tenant-create":
            t = registry.create(a.tenant, a.name)
            print(f"created tenant {t.tenant_id} ({t.tenant_type.value})")
            return 0
        if a.cmd == "tenant-list":
            for t in registry.list():
                print(f"{t.tenant_id}\t{t.tenant_type.value}\t{'active' if t.active else 'inactive'}\t{t.display_name}")
            return 0
        store = registry.store(registry.context(a.tenant))
        if a.cmd == "review-apply":
            from pipeline.book_match.review import apply_review_csv
            from pipeline.book_match.run import load_index
            index = load_index(_intel_db(a.intel_db), BookMatchConfig())
            res = apply_review_csv(store, a.csv, index, default_reviewer=a.reviewer)
            print(f"applied {res.applied} decision(s); {res.skipped_blank} blank; {len(res.issues)} issue(s)")
            for row, reason in res.issues:
                print(f"  row {row}: {reason}")
            return 0 if not res.issues else 2
        from pipeline.book_match.run import run_book_match
        cfg = BookMatchConfig.from_file(a.config) if a.config else BookMatchConfig().validate()
        s = run_book_match(store, a.csv, a.out, cfg, intel_db=_intel_db(a.intel_db))
        b = s.batch
        print(f"tenant {s.tenant_id} · batch {b.batch_id}{' (already imported)' if b.already_imported else ''}")
        print(f"rows {b.row_count} · accepted {b.accepted} · rejected {b.rejected} · warnings {b.warnings}")
        print("matches: " + ", ".join(f"{k} {v}" for k, v in s.match_counts.items()))
        print("classes: " + ", ".join(f"{k} {v}" for k, v in sorted(s.class_counts.items())))
        print(f"report: {s.report_dir}")
        return 0
    except TenantError as exc:
        print(f"tenant error: {exc}", file=sys.stderr)
        return 3
    except Exception as exc:  # report the category, never row contents
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
