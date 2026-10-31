"""Local activity page: streaks, heatmap, model split, longest stretch."""

from __future__ import annotations

from . import commands


def register(ctx) -> None:
    ctx.register_command(
        "cadence",
        handler=commands.slash_command,
        description="Show the Cadence streak and where to open the full page",
    )
    ctx.register_cli_command(
        name="cadence",
        help="Local usage streaks, heatmap, and model usage",
        setup_fn=commands.setup_argparse,
        handler_fn=commands.handle_cli,
    )
