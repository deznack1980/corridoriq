"""One CEO run: snapshot, retrieval, decision, optional write."""

from __future__ import annotations

from pathlib import Path

from agents.ceo.briefs.cursor_brief import render_cursor_brief
from agents.ceo.briefs.operating_brief import render_brief, render_status
from agents.ceo.decision_engine.engine import decide
from agents.ceo.memory.decision_log import append_decision, read_log
from agents.ceo.memory.state_store import load_latest_snapshot, save_snapshot
from agents.ceo.operating_state.snapshot import build_snapshot
from agents.ceo.paths import ensure_output_dir, output_dir, reports_generated_dir
from agents.ceo.retrieval.retriever import retrieve
from agents.ceo.security import redact
from agents.framework.limits import clip
from agents.framework.provider import ModelDisabled, ModelProvider, narration_enabled


def run(
    question: str = "What should CorridorIQ do next?",
    *,
    reports_dir: Path | None = None,
    db_path: Path | None = None,
    connect_db: bool = True,
    write: bool = True,
) -> dict:
    reports = reports_dir if reports_dir is not None else reports_generated_dir()
    previous = load_latest_snapshot() if write else None
    snapshot = build_snapshot(
        reports_dir=reports,
        db_path=db_path,
        connect_db=connect_db,
    )
    if write:
        prior = [
            row
            for row in read_log()
            if row.get("type") == "decision"
        ][-5:]
        snapshot["recent_decisions"] = [
            {
                "decision_id": row.get("decision_id"),
                "action_id": row.get("action_id"),
                "timestamp": row.get("timestamp"),
            }
            for row in prior
        ]
    retrieved = retrieve(clip(question, 2000), reports_dir=reports)
    decision = decide(snapshot, question, retrieved, previous)
    text = redact(render_brief(decision))
    narration = _maybe_narrate(decision, text)
    if narration:
        text = redact(text + "\n" + narration)
    cursor_text = None
    if decision.cursor_brief:
        cursor_text = redact(render_cursor_brief(decision.cursor_brief))
    record = None
    if write:
        root = ensure_output_dir()
        ref = save_snapshot(snapshot, root)
        record = append_decision(decision, snapshot_ref=ref)
        (root / "latest_brief.md").write_text(text, encoding="utf-8")
        if cursor_text:
            (root / "cursor_briefs" / f"{record['decision_id']}.md").write_text(
                cursor_text, encoding="utf-8"
            )
    return {
        "text": text,
        "decision": decision,
        "snapshot": snapshot,
        "cursor_text": cursor_text,
        "record": record,
    }


def run_status(*, reports_dir: Path | None = None, connect_db: bool = True) -> str:
    snapshot = build_snapshot(
        reports_dir=reports_dir if reports_dir is not None else reports_generated_dir(),
        connect_db=connect_db,
    )
    return redact(render_status(snapshot))


def _maybe_narrate(decision, text: str) -> str:
    if not narration_enabled():
        return ""
    try:
        response = ModelProvider().complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You may clarify wording only. Do not change the recommendation, "
                        "the bottleneck, or any number. Label your note as model inference."
                    ),
                },
                {"role": "user", "content": clip(text)},
            ],
            purpose="narration",
        )
    except ModelDisabled as exc:
        return f"Model narration skipped: {exc}"
    usage_path = output_dir() / "model_usage.jsonl"
    if response is not None:
        import json

        usage_path.parent.mkdir(parents=True, exist_ok=True)
        with usage_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "provider": response.provider,
                        "model": response.model,
                        "prompt_chars": response.prompt_chars,
                        "prompt_tokens": response.prompt_tokens,
                        "completion_tokens": response.completion_tokens,
                        "action_id": decision.action_id,
                    }
                )
                + "\n"
            )
    return (
        "MODEL NOTE\n"
        "Category: MODEL_INFERENCE. The recommendation was locked before this call.\n"
        + response.text
    )
