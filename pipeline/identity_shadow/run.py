"""Orchestrate one shadow run: extract → resolve → analyze → report.

Production is opened read-only (URI mode=ro + query_only); the only writes go
to the validated shadow database. Source/evidence rows are upserted with
deterministic IDs; identity, activity and analysis layers are rebuilt
wholesale, so the same inputs always yield the same shadow universe.
"""

from __future__ import annotations

import json
import logging
import time
import tracemalloc
import uuid
from datetime import datetime, timezone
from pathlib import Path

from pipeline.identity_shadow import extract as X
from pipeline.identity_shadow.analyze import analyze
from pipeline.identity_shadow.config import NORMALIZER_VERSION, PARSER_VERSION, RULES_VERSION, ShadowConfig
from pipeline.identity_shadow.report import write_reports
from pipeline.identity_shadow.resolve import resolve
from pipeline.identity_shadow.store import open_production_readonly, open_shadow

log = logging.getLogger("corridoriq.identity_shadow")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fingerprint(path) -> str:
    p = Path(path)
    st = p.stat()
    return f"size={st.st_size};mtime={int(st.st_mtime)}"


def run_shadow(production_db, shadow_db, out_dir=None, cfg: ShadowConfig | None = None,
               *, measure_memory: bool = False) -> dict:
    cfg = cfg or ShadowConfig()
    batch_id = uuid.uuid4().hex
    timings = {}
    if measure_memory:
        tracemalloc.start()
    prod = open_production_readonly(production_db)
    shadow = open_shadow(shadow_db, production_db)
    try:
        shadow.execute(
            "INSERT INTO shadow_batches (batch_id, started_at, status, parser_version, normalizer_version, "
            "rules_version, config_json, production_fingerprint) VALUES (?,?,?,?,?,?,?,?)",
            (batch_id, _now(), "running", PARSER_VERSION, NORMALIZER_VERSION, RULES_VERSION, cfg.to_json(),
             _fingerprint(production_db)))
        shadow.commit()
        counts = {}
        t0 = time.perf_counter()
        w = X._Writer(shadow, batch_id)
        with shadow:
            counts["permits"] = X.extract_permits(prod, w, cfg)
            counts["roc"] = X.extract_roc(prod, w, cfg)
            counts["roc_crosswalk"] = X.extract_roc_crosswalk(prod, w, cfg)
            counts["contact_channels"] = X.extract_contact_channels(prod, w, cfg)
            X.register_provenance(shadow)
        timings["extract_normalize"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        with shadow:
            r = resolve(shadow, cfg, batch_id)
        timings["block_resolve_materialize"] = time.perf_counter() - t0
        t0 = time.perf_counter()
        with shadow:
            analysis = analyze(shadow, prod)
        timings["analyze"] = time.perf_counter() - t0
        summary = {
            "batch_id": batch_id,
            "versions": {"parser": PARSER_VERSION, "normalizer": NORMALIZER_VERSION, "rules": RULES_VERSION},
            "extraction": counts, "resolver": dict(r.metrics), "analysis": analysis,
        }
        t0 = time.perf_counter()
        folder = write_reports(shadow, batch_id, summary, out_dir) if out_dir else None
        timings["report"] = time.perf_counter() - t0
        summary["timings"] = {k: round(v, 2) for k, v in timings.items()}
        if measure_memory:
            summary["peak_memory_mb"] = round(tracemalloc.get_traced_memory()[1] / 1048576, 1)
            tracemalloc.stop()
        summary["report_dir"] = str(folder) if folder else None
        shadow.execute("UPDATE shadow_batches SET finished_at=?, status='succeeded', counts_json=?, timings_json=? "
                       "WHERE batch_id=?", (_now(), json.dumps({"extraction": counts, "resolver": dict(r.metrics)},
                                                               default=str),
                                            json.dumps(summary["timings"]), batch_id))
        shadow.commit()
        log.info("identity_shadow run batch=%s canonical=%s timings=%s", batch_id,
                 analysis["metrics"].get("canonical_companies"), summary["timings"])
        return summary
    except Exception:
        shadow.rollback()
        shadow.execute("UPDATE shadow_batches SET finished_at=?, status='failed' WHERE batch_id=?", (_now(), batch_id))
        shadow.commit()
        raise
    finally:
        prod.close()
        shadow.close()
