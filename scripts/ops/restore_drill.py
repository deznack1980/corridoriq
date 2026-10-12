"""Restore a backup set into a disposable folder and prove it is usable.

    python scripts/ops/restore_drill.py <set-dir> [--work <empty dir>] [--migrate] [--keep]

Each step is timed:
  1. verify the set (manifest, SHA-256, integrity, row counts) — backup_set.verify_set
  2. restore into an EMPTY, non-live folder                    — backup_set.restore
  3. integrity check of every restored database
  4. expected tables per database kind
  5. aggregate counts only: no names, emails, business names or request text
  6. (--migrate) open the restored copy with THIS checkout's code and apply its
     additive migrations, proving the upgrade on real-shaped data
  7. delete the restored copy (unless --keep)

Prints a JSON report. Exit status is non-zero if any check fails.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.ops import backup_set  # noqa: E402

REPO = Path(__file__).resolve().parents[2]

# Core tables every backup must contain. Tables that the current code creates at
# startup (e.g. auth_tokens on databases backed up before that release) are
# checked after --migrate instead, and reported as schema lag, not as damage.
EXPECTED = {
    "main": {"users", "sessions", "organizations", "roles", "user_roles",
             "security_audit_log", "companies", "permits"},
    "platform": {"users", "sessions", "memberships", "contractor_profiles",
                 "request_shares", "referral_codes"},
}
CREATED_ON_START = {"main": {"auth_tokens"}, "platform": {"auth_tokens"}}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _tables(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _count(conn, sql, params=()):
    try:
        return conn.execute(sql, params).fetchone()[0]
    except sqlite3.DatabaseError:
        return None


def _group(conn, sql) -> dict:
    try:
        return {str(k): v for k, v in conn.execute(sql).fetchall()}
    except sqlite3.DatabaseError:
        return {}


def summarize_main(conn) -> dict:
    now = _now_iso()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    return {
        "users": _count(conn, "SELECT COUNT(*) FROM users"),
        "users_active": _count(conn, "SELECT COUNT(*) FROM users WHERE is_active=1"),
        "users_by_kind": _group(conn, "SELECT account_kind, COUNT(*) FROM users GROUP BY 1")
        if "account_kind" in cols else {"employee (pre-onboarding schema)": _count(conn, "SELECT COUNT(*) FROM users")},
        "role_assignments": _group(conn, "SELECT r.name, COUNT(*) FROM user_roles ur JOIN roles r ON r.id=ur.role_id GROUP BY 1"),
        "organizations": _count(conn, "SELECT COUNT(*) FROM organizations"),
        "sessions_live": _count(conn, "SELECT COUNT(*) FROM sessions WHERE revoked_at IS NULL AND expires_at > ?", (now,)),
        "auth_tokens_live_by_purpose": _group(conn, "SELECT purpose, COUNT(*) FROM auth_tokens "
                                                    f"WHERE consumed_at IS NULL AND revoked_at IS NULL AND expires_at > '{now}' GROUP BY 1"),
        "audit_events": _count(conn, "SELECT COUNT(*) FROM security_audit_log"),
        "companies": _count(conn, "SELECT COUNT(*) FROM companies"),
        "permits": _count(conn, "SELECT COUNT(*) FROM permits"),
        "crm_activities": _count(conn, "SELECT COUNT(*) FROM crm_activities"),
        "crm_tasks": _count(conn, "SELECT COUNT(*) FROM crm_tasks"),
    }


def summarize_platform(conn) -> dict:
    return {
        "users": _count(conn, "SELECT COUNT(*) FROM users"),
        "memberships_by_kind": _group(conn, "SELECT tenant_kind, COUNT(*) FROM memberships GROUP BY 1"),
        "contractor_profiles": _count(conn, "SELECT COUNT(*) FROM contractor_profiles"),
        "referral_codes_active": _count(conn, "SELECT COUNT(*) FROM referral_codes WHERE active=1"),
        "rfq_shares_by_status": _group(conn, "SELECT status, COUNT(*) FROM request_shares GROUP BY 1"),
        "rfq_share_lines": _count(conn, "SELECT COUNT(*) FROM request_share_lines"),
    }


def summarize_contractor_store(conn) -> dict:
    return {
        "material_requests_by_status": _group(conn, "SELECT status, COUNT(*) FROM material_requests GROUP BY 1"),
        "material_request_lines": _count(conn, "SELECT COUNT(*) FROM material_request_lines"),
    }


def _kind(rel: str) -> str:
    name = rel.rsplit("/", 1)[-1]
    if rel.startswith("main/"):
        return "main"
    if name == "platform.db":
        return "platform"
    if name == "contractor.db":
        return "contractor_store"
    return "other"


def migrate_with_this_code(restored: Path) -> dict:
    """Apply this checkout's additive migrations to the restored copy (subprocess)."""
    main = next((p for p in (restored / "main").glob("*.db")), None)
    pilot = restored / "pilot"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CORRIDORIQ_", "POSTMARK", "STRIPE"))}
    env.update({"CORRIDORIQ_ENV": "test", "CORRIDORIQ_CACHE_WARMER": "0",
                "CORRIDORIQ_DATA_DIR": str(restored / "_data"), "CORRIDORIQ_DB_PATH": str(main),
                "CORRIDORIQ_PILOT_ROOT": str(pilot), "CORRIDORIQ_TENANT_ROOT": str(restored / "tenants")})
    code = (
        "import json, os, sqlite3\n"
        "from pipeline.db.database import init_db, get_connection\n"
        "init_db()\n"
        "out = {}\n"
        "c = get_connection()\n"
        "out['main_integrity'] = c.execute('PRAGMA integrity_check').fetchone()[0]\n"
        "out['main_users_by_kind'] = dict(c.execute('SELECT account_kind, COUNT(*) FROM users GROUP BY 1').fetchall())\n"
        "out['main_has_auth_tokens'] = bool(c.execute(\"SELECT 1 FROM sqlite_master WHERE name='auth_tokens'\").fetchone())\n"
        "c.close()\n"
        "if os.path.exists(os.path.join(os.environ['CORRIDORIQ_PILOT_ROOT'], 'platform.db')):\n"
        "    from pipeline.pilot.platform import Platform\n"
        "    p = Platform(os.environ['CORRIDORIQ_PILOT_ROOT']).connect()\n"
        "    out['platform_integrity'] = p.execute('PRAGMA integrity_check').fetchone()[0]\n"
        "    out['platform_has_auth_tokens'] = bool(p.execute(\"SELECT 1 FROM sqlite_master WHERE name='auth_tokens'\").fetchone())\n"
        "    p.close()\n"
        "print('MIGRATE ' + json.dumps(out))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=env, capture_output=True,
                          text=True, timeout=1800)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("MIGRATE ")), None)
    if line is None:
        return {"ok": False, "error": (proc.stderr or proc.stdout)[-800:]}
    result = json.loads(line[len("MIGRATE "):])
    result["ok"] = (result.get("main_integrity") == "ok"
                    and set(result.get("main_users_by_kind", {})) <= {"employee"}
                    and result.get("main_has_auth_tokens") is True
                    and result.get("platform_integrity", "ok") == "ok"
                    and result.get("platform_has_auth_tokens", True) is True)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("set_dir", type=Path)
    parser.add_argument("--work", type=Path, help="empty disposable folder (default: new temp dir)")
    parser.add_argument("--migrate", action="store_true", help="apply this checkout's migrations to the copy")
    parser.add_argument("--keep", action="store_true", help="keep the restored copy")
    parser.add_argument("--json", type=Path, help="also write the report here")
    args = parser.parse_args(argv)

    work = args.work or Path(tempfile.mkdtemp(prefix="ciq-restore-"))
    restored = work / "restored"
    report = {"set": args.set_dir.name, "started_at": _now_iso(), "steps": {}, "databases": {}, "checks": []}
    ok = True

    def step(name, fn):
        t = time.perf_counter()
        value = fn()
        report["steps"][name] = round(time.perf_counter() - t, 2)
        return value

    try:
        problems = step("verify_set_seconds", lambda: backup_set.verify_set(args.set_dir))
        report["checks"].append({"check": "backup set verifies (checksums, integrity, rows)", "ok": not problems,
                                 "detail": problems})
        ok &= not problems
        problems = step("restore_seconds", lambda: backup_set.restore(args.set_dir, restored))
        report["checks"].append({"check": "restore into disposable folder verifies", "ok": not problems,
                                 "detail": problems})
        ok &= not problems
        manifest = json.loads((restored / backup_set.MANIFEST).read_text(encoding="utf-8"))

        def inspect_all():
            nonlocal ok
            stores = {"contractor_store": []}
            for entry in manifest["databases"]:
                rel, kind = entry["path"], _kind(entry["path"])
                conn = _ro(restored / rel)
                try:
                    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
                    tables = _tables(conn)
                    missing = sorted(EXPECTED.get(kind, set()) - tables)
                    good = integrity == "ok" and not missing
                    ok &= good
                    lag = sorted(CREATED_ON_START.get(kind, set()) - tables)
                    if lag:
                        report.setdefault("schema_lag", {})[rel] = lag
                    report["checks"].append({"check": f"{rel}: integrity + core tables", "ok": good,
                                             "detail": {"integrity": integrity, "missing_core_tables": missing}})
                    if kind == "main":
                        report["databases"][rel] = summarize_main(conn)
                    elif kind == "platform":
                        report["databases"][rel] = summarize_platform(conn)
                    elif kind == "contractor_store":
                        stores["contractor_store"].append(summarize_contractor_store(conn))
                finally:
                    conn.close()
            if stores["contractor_store"]:
                agg, lines = {}, 0
                for s in stores["contractor_store"]:
                    for k, v in s["material_requests_by_status"].items():
                        agg[k] = agg.get(k, 0) + v
                    lines += s["material_request_lines"] or 0
                report["databases"]["contractor stores (aggregate)"] = {
                    "stores": len(stores["contractor_store"]), "material_requests_by_status": agg,
                    "material_request_lines": lines}

        step("inspect_seconds", inspect_all)
        if args.migrate:
            result = step("migrate_seconds", lambda: migrate_with_this_code(restored))
            report["migration_with_this_code"] = result
            report["checks"].append({"check": "this code's additive migrations apply to the restored copy",
                                     "ok": result.get("ok", False)})
            ok &= bool(result.get("ok"))
    finally:
        report["total_seconds"] = round(sum(report["steps"].values()), 2)
        if not args.keep:
            shutil.rmtree(work if args.work is None else restored, ignore_errors=True)
            report["restored_copy_deleted"] = not restored.exists()

    text = json.dumps(report, indent=2, default=str)
    print(text)
    if args.json:
        args.json.write_text(text, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
