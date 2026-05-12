"""Shared bridge runtime state and helpers used by command handlers.

This module owns process-wide state that must be visible to both bridge.py's
message dispatcher and the per-command handlers in patchbay/commands/. Kept
separate from bridge.py to break the cycle: commands import from runtime,
bridge imports from runtime; neither imports from the other at module load
time. `send_response` defers its `bridge` import to call time so the cycle
never fires during startup.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

# Re-exports from existing patchbay modules — pure pass-through so command
# modules can import every runtime symbol they need from one place.
from patchbay.sessions import (  # noqa: F401
    clear_session,
    get_session_id,
    save_session_id,
)

# `set_session_id` is the runtime-public alias for `save_session_id` —
# command handlers refer to "set" semantics; the persistence layer
# spells it "save". Both names point at the same function.
set_session_id = save_session_id

if TYPE_CHECKING:
    from bridge import SessionState

# Captured once at import time. Used by /health and /ping to report uptime.
BRIDGE_STARTED_AT: float = time.time()

# Process-wide session registry, keyed by session_key (chat_id:thread_id).
# bridge.py imports this as `_sessions` to keep its existing call sites
# unchanged; patchbay/commands/* imports it directly as `sessions`.
sessions: dict[str, "SessionState"] = {}


def iter_active_sessions() -> list[tuple[str, "SessionState"]]:
    """Snapshot of (session_key, state) for every session running a turn,
    regardless of harness. Used by /ping, the stall detector, /restart,
    and graceful shutdown so SDK turns are visible too.

    A session counts as active when either `state.proc` is set
    (subprocess harnesses) or `state.harness` is set (SDK harnesses).
    Either signal alone is sufficient.
    """
    return [
        (k, s)
        for k, s in sessions.items()
        if s.proc is not None or s.harness is not None
    ]


async def send_response(bot: Any, chat_id: int, thread_id: int | None, response: str) -> None:
    """Send a (possibly chunked, possibly markdown-downgraded) response to Telegram.

    Delegates to bridge._send_response. The import is deferred to call time
    so module load doesn't create a bridge → runtime → bridge cycle.
    """
    from bridge import _send_response

    return await _send_response(bot, chat_id, thread_id, response)
