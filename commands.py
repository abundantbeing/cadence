"""``hermes cadence`` and ``/cadence``."""

from __future__ import annotations

import json
import sys

from . import activity_board as data

HELP = """Usage: /cadence
Open Cadence in Hermes Desktop for the streak, heatmap, and model split.
"""


def slash_command(_raw_args: str = "") -> str:
    try:
        report = data.summary()
    except data.ActivityError as exc:
        return f"cadence: {exc}"
    streaks = report["streaks"]
    totals = report["totals"]
    top = report["models"][0]["model"] if report["models"] else "none yet"
    return (
        f"Cadence: {streaks['current']} day current streak, "
        f"{streaks['longest']} day longest, {totals['tokens']:,} tokens. "
        f"Most used model: {top}. Open Cadence in the sidebar for the full grid."
    )


def setup_argparse(subparser) -> None:
    subs = subparser.add_subparsers(dest="activity_command")
    summary = subs.add_parser("summary", help="Streaks, heatmap, and model totals")
    summary.add_argument("--json", action="store_true")
    chosen = subs.add_parser("day", help="Sessions active on one local day")
    chosen.add_argument("date")
    chosen.add_argument("--json", action="store_true")
    subparser.set_defaults(func=handle_cli)


def handle_cli(args) -> None:
    try:
        if args.activity_command == "day":
            payload = data.day(args.date)
        else:
            payload = data.summary()
    except data.ActivityError as exc:
        payload = {"error": str(exc)}
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
