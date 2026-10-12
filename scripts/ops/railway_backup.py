"""Pull a consistent, read-only backup of every CorridorIQ SQLite store out of a
running Railway service, then restore-test it locally.

    python scripts/ops/railway_backup.py command inventory   # print the exact railway command
    python scripts/ops/railway_backup.py command stream
    python scripts/ops/railway_backup.py receive <stream-file> --out <dir>
    python scripts/ops/railway_backup.py selftest            # local end-to-end check, synthetic data

The production image does not contain this repository's ops tools, so the code
that runs inside the container (CONTAINER_SCRIPT) is self-contained and uses
the standard library only. It is passed to ``python3 -c`` over ``railway ssh``.

Safety properties of CONTAINER_SCRIPT:
* every database is opened ``mode=ro``; ``VACUUM INTO`` reads a consistent
  snapshot (WAL included) without blocking writers or changing the source;
* copies are written to the container's temporary disk, never to the volume,
  and deleted before the script exits;
* ``inventory`` prints paths, sizes, mount points, journal modes and free
  space only — never row data, names, emails or secrets;
* ``stream`` prints a base64 tar.gz between markers, followed by its SHA-256,
  so any transport corruption is detected by ``receive``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import shlex
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

BEGIN, END = "CIQ-BACKUP-BEGIN", "CIQ-BACKUP-END"

CONTAINER_SCRIPT = r'''
import base64, hashlib, io, json, os, shutil, sqlite3, sys, tarfile, tempfile
from datetime import datetime, timezone
from pathlib import Path

def roots():
    db = os.environ.get("CORRIDORIQ_DB_PATH", "").strip()
    if db:
        main = Path(db).expanduser()
    else:
        d = os.environ.get("CORRIDORIQ_DATA_DIR", "").strip()
        main = (Path(d).expanduser() if d else Path.home() / ".local" / "share" / "corridoriq") / "db" / "corridoriq.db"
    out = {"main": main}
    for label, env in (("pilot", "CORRIDORIQ_PILOT_ROOT"), ("tenants", "CORRIDORIQ_TENANT_ROOT")):
        v = os.environ.get(env, "").strip()
        if v:
            out[label] = Path(v).expanduser()
    return out

def discover():
    found = []
    for label, root in roots().items():
        if label == "main":
            if root.is_file():
                found.append(("main/" + root.name, root))
        elif root.is_dir():
            for db in sorted(root.rglob("*.db")):
                found.append((label + "/" + db.relative_to(root).as_posix(), db))
    return found

def mount_of(path):
    best, fstype = "/", "?"
    try:
        for line in open("/proc/mounts"):
            parts = line.split()
            mp = parts[1]
            if str(path).startswith(mp.rstrip("/") + "/") or str(path) == mp:
                if len(mp) > len(best):
                    best, fstype = mp, parts[2]
    except OSError:
        pass
    return best, fstype

def inventory():
    report = {"roots": {}, "databases": [], "tmp_free_bytes": shutil.disk_usage(tempfile.gettempdir()).free}
    for label, root in roots().items():
        mp, fs = mount_of(root)
        report["roots"][label] = {"path": str(root), "exists": root.exists(), "mount_point": mp,
                                  "filesystem": fs, "on_root_filesystem": mp == "/",
                                  "free_bytes": shutil.disk_usage(root if root.is_dir() else root.parent).free
                                  if (root if root.is_dir() else root.parent).exists() else None}
    total = 0
    for rel, path in discover():
        c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            jm = c.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            c.close()
        size = path.stat().st_size
        total += size
        report["databases"].append({"name": rel, "bytes": size, "journal_mode": jm})
    report["database_count"] = len(report["databases"])
    report["total_bytes"] = total
    print(json.dumps(report, indent=2))

def stream():
    work = Path(tempfile.mkdtemp(prefix="ciq-backup-"))
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        set_dir = work / f"corridoriq_set_{stamp}"
        entries = []
        for rel, path in discover():
            target = set_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                src.execute("VACUUM INTO ?", (str(target),))
            finally:
                src.close()
            chk = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
            try:
                if chk.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise SystemExit(f"copy of {rel} failed integrity check")
                tables = [r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                rows = sum(chk.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables)
            finally:
                chk.close()
            h = hashlib.sha256(target.read_bytes()).hexdigest()
            entries.append({"path": rel, "bytes": target.stat().st_size, "rows": rows, "sha256": h})
        (set_dir / "manifest.json").write_text(json.dumps({"created_at": stamp, "databases": entries,
                                                           "count": len(entries)}, indent=2))
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(set_dir, arcname=set_dir.name)
        data = buf.getvalue()
        sys.stdout.write("CIQ-BACKUP-BEGIN\n")
        enc = base64.b64encode(data).decode("ascii")
        for i in range(0, len(enc), 76):
            sys.stdout.write(enc[i:i + 76] + "\n")
        sys.stdout.write("CIQ-BACKUP-END sha256=" + hashlib.sha256(data).hexdigest() + "\n")
        sys.stdout.flush()
    finally:
        shutil.rmtree(work, ignore_errors=True)

{"inventory": inventory, "stream": stream}[sys.argv[1] if len(sys.argv) > 1 else "inventory"]()
'''


def railway_command(mode: str, service: str | None = None) -> str:
    """The command the operator runs (or approves) from a machine logged in to Railway."""
    svc = f" --service {shlex.quote(service)}" if service else ""
    return f"railway ssh{svc} -- python3 -c {shlex.quote(CONTAINER_SCRIPT)} {mode}"


def receive(stream_file: Path, out: Path) -> Path:
    from scripts.ops import backup_set
    lines = [l.strip().replace("\r", "") for l in stream_file.read_text(encoding="utf-8", errors="replace").splitlines()]
    try:
        start = lines.index(BEGIN)
        end = next(i for i, l in enumerate(lines) if l.startswith(END))
    except (ValueError, StopIteration):
        raise SystemExit("stream markers not found; the transfer is incomplete")
    data = base64.b64decode("".join(lines[start + 1:end]))
    expected = lines[end].split("sha256=", 1)[1].strip()
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit("stream checksum mismatch; the transfer was corrupted")
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        names = tar.getnames()
        top = {n.split("/", 1)[0] for n in names}
        if len(top) != 1 or any(n.startswith(("/", "..")) or "/../" in n for n in names):
            raise SystemExit("unexpected archive layout")
        if sys.version_info >= (3, 12):
            tar.extractall(out, filter="data")
        else:  # pragma: no cover
            tar.extractall(out)
    set_dir = out / top.pop()
    problems = backup_set.verify_set(set_dir)
    if problems:
        raise SystemExit("received set failed verification: " + "; ".join(problems))
    return set_dir


def selftest() -> int:
    """Run CONTAINER_SCRIPT locally against synthetic roots, receive it, and restore-drill it."""
    work = Path(tempfile.mkdtemp(prefix="ciq-rwb-selftest-"))
    env = {k: v for k, v in os.environ.items() if not k.startswith("CORRIDORIQ_")}
    env.update({"CORRIDORIQ_ENV": "test", "CORRIDORIQ_CACHE_WARMER": "0",
                "CORRIDORIQ_DATA_DIR": str(work / "live"), "CORRIDORIQ_DB_PATH": str(work / "live" / "db" / "corridoriq.db"),
                "CORRIDORIQ_PILOT_ROOT": str(work / "live" / "pilot"),
                "CORRIDORIQ_TENANT_ROOT": str(work / "live" / "tenants")})
    (work / "live" / "pilot").mkdir(parents=True)
    (work / "live" / "tenants").mkdir(parents=True)
    build = ("from pipeline.db.database import init_db; init_db()\n"
             "import os\nfrom pipeline.pilot.platform import Platform\n"
             "p = Platform(os.environ['CORRIDORIQ_PILOT_ROOT']); p.connect().close()\n"
             "t = p.contractors.create('Synthetic Co'); s = p.contractors.store(p.contractors.context(t.tenant_id))\n"
             "s.ensure_schema('CREATE TABLE IF NOT EXISTS material_requests (request_id TEXT PRIMARY KEY, status TEXT)')\n"
             "with s.connect() as c:\n"
             "    c.execute(\"INSERT INTO material_requests VALUES ('r-0000000000000001','SENT')\"); c.commit()\n")
    repo = Path(__file__).resolve().parents[2]
    setup = subprocess.run([sys.executable, "-c", build], cwd=repo, env=env, capture_output=True, text=True)
    if setup.returncode != 0:
        raise SystemExit("selftest setup failed:\n" + setup.stderr[-1500:])
    inv = subprocess.run([sys.executable, "-c", CONTAINER_SCRIPT, "inventory"], env=env, capture_output=True, text=True, check=True)
    print("inventory:", json.loads(inv.stdout)["database_count"], "databases")
    stream_file = work / "stream.txt"
    with open(stream_file, "w", encoding="utf-8") as fh:
        subprocess.run([sys.executable, "-c", CONTAINER_SCRIPT, "stream"], env=env, stdout=fh, check=True)
    set_dir = receive(stream_file, work / "received")
    drill = subprocess.run([sys.executable, str(repo / "scripts" / "ops" / "restore_drill.py"), str(set_dir)],
                           cwd=repo, env=env, capture_output=True, text=True)
    report = json.loads(drill.stdout)
    for check in report["checks"]:
        print(("PASS " if check["ok"] else "FAIL ") + check["check"])
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    return drill.returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_cmd = sub.add_parser("command")
    p_cmd.add_argument("mode", choices=["inventory", "stream"])
    p_cmd.add_argument("--service")
    p_recv = sub.add_parser("receive")
    p_recv.add_argument("stream_file", type=Path)
    p_recv.add_argument("--out", required=True, type=Path)
    sub.add_parser("selftest")
    args = parser.parse_args(argv)
    if args.cmd == "command":
        print(railway_command(args.mode, args.service))
        return 0
    if args.cmd == "receive":
        print(receive(args.stream_file, args.out))
        return 0
    return selftest()


if __name__ == "__main__":
    sys.exit(main())
