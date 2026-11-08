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
    across = f" across {len(report['included_profiles'])} profiles" if report.get("included_profiles") else ""
    return (
        f"Cadence{across}: {streaks['current']} day current streak, "
        f"{streaks['longest']} day longest, {totals['tokens']:,} tokens. "
        f"Most used model: {top}. Open Cadence in the sidebar for the full grid."
    )


def setup_argparse(subparser) -> None:
    subs = subparser.add_subparsers(dest="activity_command")
    summary = subs.add_parser("summary", help="Streaks, heatmap, and model totals")
    summary.add_argument("--json", action="store_true")
    summary.add_argument("--scope", choices=data.SCOPES, help="This profile, or every profile on this machine")
    chosen = subs.add_parser("day", help="Sessions active on one local day")
    chosen.add_argument("date")
    chosen.add_argument("--json", action="store_true")
    chosen.add_argument("--scope", choices=data.SCOPES, help="This profile, or every profile on this machine")
    subparser.set_defaults(func=handle_cli)


def handle_cli(args) -> None:
    try:
        scope = getattr(args, "scope", None)
        if args.activity_command == "day":
            payload = data.day(args.date, scope=scope)
        else:
            payload = data.summary(scope=scope)
    except data.ActivityError as exc:
        payload = {"error": str(exc)}
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
