"""Identity-evidence shadow CLI.

    python -m pipeline.identity_shadow --shadow-db <path.db> --out <report dir>
        [--production-db <path>] [--as-of YYYY-MM-DD] [--no-history]

Production (default settings.DB_PATH) is opened read-only. The shadow
database path must be given explicitly; it is refused if it is (or looks
like) the production database.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from pipeline.identity_shadow.config import ShadowConfig
from pipeline.identity_shadow.store import ShadowPathError


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m pipeline.identity_shadow")
    p.add_argument("--shadow-db", required=True)
    p.add_argument("--out")
    p.add_argument("--production-db")
    p.add_argument("--as-of")
    p.add_argument("--no-history", action="store_true")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    from pipeline.config import settings
    from pipeline.identity_shadow.run import run_shadow
    cfg = ShadowConfig(as_of=a.as_of, include_history=not a.no_history)
    try:
        s = run_shadow(a.production_db or settings.DB_PATH, a.shadow_db, a.out, cfg, measure_memory=True)
    except ShadowPathError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"batch": s["batch_id"], "diff": s["analysis"]["diff"], "timings": s["timings"],
                      "peak_memory_mb": s.get("peak_memory_mb"), "report": s["report_dir"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
