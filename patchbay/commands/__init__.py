"""Telegram command handlers, grouped by concern.

Each submodule exports `cmd_*` async handlers; `register_handlers` wires them
all onto a telegram.ext.Application instance. Called once from bridge.main().

Handlers reference bridge module attributes via `import bridge` + `bridge.X`
instead of `from bridge import X` so test patches against `bridge.X` are
visible (e.g. `patch.object(bridge, "_log_activity")`). Module-load order:
bridge.py only imports `patchbay.commands` from inside main(), so the
top-level `import bridge` here doesn't form a load-time cycle.
"""

from telegram.ext import Application, CommandHandler

from patchbay.commands import lifecycle


def register_handlers(app: Application) -> None:
    """Wire every cmd_* handler onto the Application."""
    # Lifecycle group
    app.add_handler(CommandHandler("start", lifecycle.cmd_start))
    app.add_handler(CommandHandler("clearnew", lifecycle.cmd_clearnew))
    app.add_handler(CommandHandler("cancel", lifecycle.cmd_cancel))
    app.add_handler(CommandHandler("kill", lifecycle.cmd_kill))
    app.add_handler(CommandHandler("restart", lifecycle.cmd_restart))
    app.add_handler(CommandHandler("ping", lifecycle.cmd_ping))
