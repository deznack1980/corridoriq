"""CLI for the read-only CorridorIQ CEO agent."""

from __future__ import annotations

import argparse
import sys

from agents.ceo.evals.evaluator import evaluate_all, format_results
from agents.ceo.memory.decision_log import render_log
from agents.ceo.service import run, run_status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agents.ceo")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Read-only operating snapshot. Does not log a decision.")
    sub.add_parser("brief", help="CEO brief for the current operating state.")

    ask = sub.add_parser("ask", help="Ask the CEO a question against the current state.")
    ask.add_argument("question")

    challenge = sub.add_parser("challenge", help="Challenge a proposed action.")
    challenge.add_argument("question")

    cursor = sub.add_parser(
        "cursor-brief",
        help="Print a Cursor brief only when engineering is recommended.",
    )
    cursor.add_argument("question", nargs="?", default="What should CorridorIQ do next?")

    sub.add_parser("decisions", help="Show the append-only decision log.")
    sub.add_parser("eval", help="Run the packaged benchmark scenarios.")

    morning = sub.add_parser(
        "morning",
        help="Read-only Morning Operator brief. Writes only under reports/generated/ceo/morning/.",
    )
    morning.add_argument("--as-of", help="Evaluate as of this UTC date (YYYY-MM-DD). Default: now.")
    morning.add_argument("--db", help="Database path. Default: pipeline settings DB_PATH.")
    morning.add_argument("--no-write", action="store_true", help="Print only; write no artifacts.")

    outcome = sub.add_parser("outcome", help="Record a human outcome for a morning opportunity.")
    outcome.add_argument("opportunity_id")
    outcome.add_argument("outcome")
    outcome.add_argument("--action", help="What the human did (CALL_NOW, EMAIL, RESEARCH, ...).")
    outcome.add_argument("--note")
    outcome.add_argument("--by", dest="recorded_by")

    args = parser.parse_args(argv)
    if args.command == "morning":
        return _morning(args)
    if args.command == "outcome":
        return _outcome(args)
    if args.command == "status":
        _emit(run_status())
        return 0
    if args.command == "decisions":
        _emit(render_log())
        return 0
    if args.command == "eval":
        from agents.ceo.evals.morning_eval import evaluate_morning

        results = evaluate_all() + evaluate_morning()
        _emit(format_results(results))
        return 0 if all(item["passed"] for item in results) else 1
    if args.command == "brief":
        question = "What should CorridorIQ do next?"
    elif args.command in {"ask", "challenge", "cursor-brief"}:
        question = args.question
    else:
        return 2
    result = run(question)
    if args.command == "cursor-brief":
        if result["cursor_text"]:
            _emit(result["cursor_text"])
        else:
            _emit(result["text"])
        return 0
    _emit(result["text"])
    return 0


def _morning(args) -> int:
    from pathlib import Path

    from agents.ceo.morning.dates import phoenix_day
    from agents.ceo.morning.operator import run_morning

    as_of = None
    if args.as_of:
        as_of = phoenix_day(args.as_of)
        if as_of is None:
            _emit(f"Unreadable --as-of {args.as_of!r}; use YYYY-MM-DD.")
            return 2
    result = run_morning(
        db_path=Path(args.db) if args.db else None,
        as_of=as_of,
        write=not args.no_write,
    )
    _emit(result["text"])
    for name, path in (result.get("paths") or {}).items():
        _emit(f"wrote {name}: {path}")
    return 0


def _outcome(args) -> int:
    from agents.ceo.morning.outcomes import record_outcome

    try:
        row = record_outcome(
            args.opportunity_id,
            args.outcome,
            action=args.action,
            note=args.note,
            recorded_by=args.recorded_by,
        )
    except (ValueError, LookupError) as exc:
        _emit(str(exc))
        return 2
    _emit(f"Recorded {row['outcome']} for {row['opportunity_id']} ({row['company']}).")
    return 0


def _emit(text: str) -> None:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
