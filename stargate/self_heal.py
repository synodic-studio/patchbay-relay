"""Self-healing failure dispatcher.

Centralizes the "try to fix it ourselves before paging" pattern from
docs/STARGATE-IMPROVEMENT-PLAN.md §1.5 and Fanta's "Self-Heal Before
Alerting" rule.

API
---

    result = dispatch_repair(kind, context)
    if result.fixed:
        # caller can retry / resume
    else:
        # caller pages the user as a last resort

Three failure classes are handled today:

    "corrupt_session_json"
        Quarantine the bad file via config.quarantine_file and let the
        caller start a fresh session.
        context: {"path": Path, "reason": str}

    "stale_telegram_poller"
        Another bridge process is holding the Telegram getUpdates lane
        and producing 409 Conflict errors. Signal the stale PID to exit
        so we can take over.
        context: {} (reads PID from singleton lockfile)

    "claude_oom_137"
        `claude -p` was OOM-killed (exit 137 or signal -9). The caller
        should retry once with a smaller turn budget and trimmed prompt;
        this handler returns the recommended retry parameters in
        result.actions metadata.
        context: {"session_key": str, "returncode": int}

Adding a new handler:

    @register_handler("my_failure_kind")
    def _repair_my_failure(ctx: dict) -> RepairResult:
        ...
        return RepairResult(fixed=True, kind="my_failure_kind", actions=[...])

Every dispatch is logged to activity.jsonl as a `self_heal` event so
the outcome (fixed / not fixed / handler missing) is auditable later.
"""

from __future__ import annotations

import signal
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .activity import log_activity
from .config import logger, quarantine_file


@dataclass
class RepairResult:
    """The outcome of a dispatch_repair call.

    fixed: True if the failure was repaired (or its effect contained).
           Callers can then retry the original operation.
    kind: The failure-class string the handler was invoked for.
    actions: Human-readable list of what the handler did (or recommends).
    error: A short error string if the repair attempt itself failed.
    """

    fixed: bool
    kind: str
    actions: list[str] = field(default_factory=list)
    error: str | None = None


_HANDLERS: dict[str, Callable[[dict], RepairResult]] = {}


def register_handler(kind: str) -> Callable[[Callable[[dict], RepairResult]], Callable]:
    """Decorator: register a repair handler for a failure kind."""

    def deco(fn: Callable[[dict], RepairResult]) -> Callable[[dict], RepairResult]:
        _HANDLERS[kind] = fn
        return fn

    return deco


def dispatch_repair(kind: str, context: dict | None = None) -> RepairResult:
    """Route a failure to its registered handler. Always logs to activity.jsonl.

    Returns a RepairResult; never raises (handler exceptions are caught and
    surfaced via result.error). Callers decide what to do based on
    result.fixed.
    """
    ctx = context or {}
    handler = _HANDLERS.get(kind)

    if handler is None:
        result = RepairResult(
            fixed=False,
            kind=kind,
            actions=[],
            error=f"no handler registered for {kind!r}",
        )
        logger.warning("self_heal: no handler for %s", kind)
    else:
        try:
            result = handler(ctx)
        except Exception as e:  # noqa: BLE001 — handler is untrusted; report any failure
            logger.exception("self_heal handler for %s raised", kind)
            result = RepairResult(fixed=False, kind=kind, actions=[], error=str(e))

    log_activity(
        "self_heal",
        kind=result.kind,
        fixed=result.fixed,
        actions=result.actions,
        error=result.error,
    )
    return result


# ---------------------------------------------------------------------------
# Built-in handlers
# ---------------------------------------------------------------------------


@register_handler("corrupt_session_json")
def _repair_corrupt_session_json(ctx: dict) -> RepairResult:
    """Quarantine a corrupt JSON state file. Caller restarts the session."""
    path = ctx.get("path")
    reason = ctx.get("reason", "unspecified")
    if not isinstance(path, (str, Path)):
        return RepairResult(
            fixed=False,
            kind="corrupt_session_json",
            actions=[],
            error="missing or invalid 'path' in context",
        )
    path = Path(path)
    if not path.exists():
        # Already cleaned up — nothing to do, but it's effectively fixed.
        return RepairResult(
            fixed=True,
            kind="corrupt_session_json",
            actions=[f"{path} already gone"],
        )

    new_loc = quarantine_file(path, reason)
    if new_loc is None:
        return RepairResult(
            fixed=False,
            kind="corrupt_session_json",
            actions=["quarantine attempted"],
            error="quarantine_file returned None",
        )
    return RepairResult(
        fixed=True,
        kind="corrupt_session_json",
        actions=[f"quarantined {path.name} -> {new_loc}"],
    )


@register_handler("stale_telegram_poller")
def _repair_stale_telegram_poller(ctx: dict) -> RepairResult:
    """Signal a stale bridge instance to exit so we can take over polling.

    The singleton lockfile records the active bridge's PID. If we're seeing
    409 Conflict storms, something else is also calling getUpdates — most
    likely a previous bridge process that didn't release. Send SIGTERM and
    let it exit cleanly. The caller's next getUpdates poll should succeed.
    """
    # Imported lazily so this module stays importable when singleton state
    # isn't relevant (tests, scripts).
    from .singleton import signal_other_bridge

    sig = ctx.get("signal", signal.SIGTERM)
    pid = signal_other_bridge(sig)
    if pid is None:
        return RepairResult(
            fixed=False,
            kind="stale_telegram_poller",
            actions=["no other bridge PID found in lockfile"],
        )
    return RepairResult(
        fixed=True,
        kind="stale_telegram_poller",
        actions=[f"sent {sig} to pid {pid}"],
    )


# Recommended retry parameters for an OOM-killed claude run. Surfaced via
# RepairResult.actions so the caller (run_claude) doesn't have to re-derive.
OOM_RETRY_MAX_TURNS = 50
OOM_RETRY_PROMPT_TRIM = 1000


@register_handler("claude_oom_137")
def _repair_claude_oom(ctx: dict) -> RepairResult:
    """Confirm OOM and recommend retry parameters.

    This handler doesn't *do* the retry — that has to happen in
    bridge.run_claude where Popen is wired. It just validates the caller's
    diagnosis and returns the recommended budget so the retry path is
    consistent across call sites.
    """
    returncode = ctx.get("returncode")
    if returncode not in (137, -9):
        return RepairResult(
            fixed=False,
            kind="claude_oom_137",
            actions=[],
            error=f"returncode {returncode!r} is not OOM-shaped (expected 137 or -9)",
        )
    session_key = ctx.get("session_key", "?")
    return RepairResult(
        fixed=True,
        kind="claude_oom_137",
        actions=[
            f"recommend retry for {session_key} with "
            f"max_turns={OOM_RETRY_MAX_TURNS}, "
            f"prompt_trim={OOM_RETRY_PROMPT_TRIM} chars",
        ],
    )


# ---------------------------------------------------------------------------
# Test/diagnostic helpers
# ---------------------------------------------------------------------------


def list_kinds() -> list[str]:
    """Return all registered failure kinds (sorted)."""
    return sorted(_HANDLERS)


def reset_handlers() -> None:
    """Wipe the handler registry. Tests use this to inject mocks."""
    _HANDLERS.clear()


