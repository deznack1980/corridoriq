"""End-to-end offline run for one tenant: import → match → classify → report."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from pipeline.book_match.classify import classify_run
from pipeline.book_match.config import BookMatchConfig
from pipeline.book_match.importer import ImportResult, import_book
from pipeline.book_match.intel import IntelIndex, open_readonly
from pipeline.book_match.matcher import match_book
from pipeline.book_match.report import generate_reports

log = logging.getLogger("corridoriq.book_match")


@dataclass
class RunSummary:
    tenant_id: str
    batch: ImportResult
    run_id: str
    match_counts: dict
    class_counts: dict
    report_dir: Path
    timings: dict


def load_index(intel_db, cfg: BookMatchConfig) -> IntelIndex:
    conn = open_readonly(intel_db)
    try:
        return IntelIndex.load(conn, as_of=cfg.as_of_date(), window_days=cfg.market_window_days)
    finally:
        conn.close()


def run_book_match(store, csv_path, out_dir, cfg: BookMatchConfig, *, index: IntelIndex | None = None,
                   intel_db=None) -> RunSummary:
    cfg.validate()
    t = {}
    t0 = time.perf_counter()
    batch = import_book(store, csv_path, today=cfg.as_of_date())
    t["import"] = time.perf_counter() - t0
    if index is None:
        if intel_db is None:
            raise ValueError("an intelligence database or index is required")
        t0 = time.perf_counter()
        index = load_index(intel_db, cfg)
        t["index_load"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    run_id, match_counts = match_book(store, index, cfg, batch.batch_id)
    t["match"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    class_counts = classify_run(store, run_id, index, cfg)
    t["classify"] = time.perf_counter() - t0
    t0 = time.perf_counter()
    folder = generate_reports(store, run_id, out_dir, index)
    t["report"] = time.perf_counter() - t0
    log.info("book_match run tenant=%s run=%s timings=%s", store.tenant_id, run_id,
             {k: round(v, 2) for k, v in t.items()})
    return RunSummary(store.tenant_id, batch, run_id, match_counts, class_counts, folder, t)
