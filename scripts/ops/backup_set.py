"""Consistent backup set of EVERY CorridorIQ SQLite database, plus restore drill.

    python scripts/ops/backup_set.py create  --out <dir>
    python scripts/ops/backup_set.py verify  <set-dir>
    python scripts/ops/backup_set.py restore <set-dir> --dest <empty dir>

``pipeline.db.backup`` covers only the intelligence database (DB_PATH). The
contractor pilot keeps accounts in ``<pilot root>/platform.db`` and every
contractor's requests in its own ``contractor_tenants/<id>/contractor.db``;
supplier tenants keep ``<id>/tenant.db``. None of those were backed up. This
tool discovers all of them and copies each with ``VACUUM INTO`` (consistent
while the server runs, WAL included), then integrity-checks every copy and
writes ``manifest.json`` with SHA-256, row counts and source-relative paths.

Restore never writes over a live database: ``--dest`` must be empty (or
absent) and must not be, or sit inside, any configured live root.

Roots come from the same settings the server uses: ``CORRIDORIQ_DB_PATH`` /
``CORRIDORIQ_DATA_DIR``, ``CORRIDORIQ_PILOT_ROOT``, ``CORRIDORIQ_TENANT_ROOT``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.config import settings  # noqa: E402

MANIFEST = "manifest.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def live_roots() -> dict[str, Path]:
    """Label -> live root directory (or file, for the main DB)."""
    roots = {"main": Path(settings.DB_PATH)}
    for label, env in (("pilot", "CORRIDORIQ_PILOT_ROOT"), ("tenants", "CORRIDORIQ_TENANT_ROOT")):
        value = (os.environ.get(env) or "").strip()
        if value:
            roots[label] = Path(value).expanduser()
    return roots


def discover() -> list[tuple[str, Path]]:
    """(relative name inside the set, live path) for every database."""
    found = []
    for label, root in live_roots().items():
        if label == "main":
            if root.is_file():
                found.append(("main/" + root.name, root))
            continue
        if root.is_dir():
            for db in sorted(root.rglob("*.db")):
                found.append((f"{label}/{db.relative_to(root).as_posix()}", db))
    return found


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def check(path: Path) -> tuple[bool, str, int]:
    """(ok, integrity detail, total rows) opened read-only."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return False, f"cannot open: {exc}", 0
    try:
        detail = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if detail != "ok":
            return False, detail, 0
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return True, "ok", sum(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables)
    except sqlite3.DatabaseError as exc:
        return False, str(exc), 0
    finally:
        conn.close()


def create(out: Path) -> Path:
    databases = discover()
    if not databases:
        raise SystemExit("no databases found under the configured roots")
    set_dir = out / f"corridoriq_set_{_now():%Y%m%dT%H%M%SZ}"
    if set_dir.exists():
        raise SystemExit(f"backup set already exists: {set_dir}")
    entries = []
    for rel, live in databases:
        target = set_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(f"file:{live}?mode=ro", uri=True)
        try:
            conn.execute("VACUUM INTO ?", (str(target),))
        finally:
            conn.close()
        ok, detail, rows = check(target)
        if not ok:
            raise SystemExit(f"backup of {rel} failed verification ({detail}); set is incomplete")
        entries.append({"path": rel, "bytes": target.stat().st_size, "rows": rows,
                        "sha256": _sha256(target)})
    manifest = {"created_at": _now().isoformat(timespec="seconds"),
                "databases": entries, "count": len(entries)}
    (set_dir / MANIFEST).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return set_dir


def verify_set(set_dir: Path) -> list[str]:
    manifest = json.loads((set_dir / MANIFEST).read_text(encoding="utf-8"))
    problems = []
    for entry in manifest["databases"]:
        path = set_dir / entry["path"]
        if not path.is_file():
            problems.append(f"{entry['path']}: missing")
            continue
        if _sha256(path) != entry["sha256"]:
            problems.append(f"{entry['path']}: checksum mismatch")
        ok, detail, rows = check(path)
        if not ok:
            problems.append(f"{entry['path']}: integrity {detail}")
        elif rows != entry["rows"]:
            problems.append(f"{entry['path']}: rows {rows} != manifest {entry['rows']}")
    return problems


def _overlaps_live(dest: Path) -> bool:
    dest = dest.resolve()
    for root in live_roots().values():
        root = root.resolve()
        live_dir = root.parent if root.suffix == ".db" else root
        if dest == live_dir or live_dir in dest.parents or dest in live_dir.parents:
            return True
    return False


def restore(set_dir: Path, dest: Path) -> list[str]:
    if _overlaps_live(dest):
        raise SystemExit("refusing to restore into or around a live database root")
    if dest.exists() and any(dest.iterdir()):
        raise SystemExit("--dest must be empty or absent; restore never overwrites")
    problems = verify_set(set_dir)
    if problems:
        raise SystemExit("backup set failed verification: " + "; ".join(problems))
    manifest = json.loads((set_dir / MANIFEST).read_text(encoding="utf-8"))
    for entry in manifest["databases"]:
        target = dest / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(set_dir / entry["path"], target)
    shutil.copy2(set_dir / MANIFEST, dest / MANIFEST)
    return verify_set(dest)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_create = sub.add_parser("create")
    p_create.add_argument("--out", required=True, type=Path)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("set_dir", type=Path)
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("set_dir", type=Path)
    p_restore.add_argument("--dest", required=True, type=Path)
    args = parser.parse_args(argv)

    if args.cmd == "create":
        set_dir = create(args.out)
        manifest = json.loads((set_dir / MANIFEST).read_text(encoding="utf-8"))
        print(f"backup set {set_dir.name}: {manifest['count']} database(s)")
        for entry in manifest["databases"]:
            print(f"  {entry['path']}  {entry['bytes']:,} bytes  rows={entry['rows']:,}")
        return 0
    if args.cmd == "verify":
        problems = verify_set(args.set_dir)
        print("PASS" if not problems else "FAIL: " + "; ".join(problems))
        return 0 if not problems else 1
    problems = restore(args.set_dir, args.dest)
    print("restored and verified" if not problems else "FAIL: " + "; ".join(problems))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
