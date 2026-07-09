#!/usr/bin/env python3
"""Claude Code Telegram Bridge

Thin relay: Telegram messages -> claude -p -> Telegram responses.
Claude Code is the brain. This script is just a phone line.

Conversation continuity: each forum topic (or DM chat) maintains its own
session ID so consecutive messages share context. Sessions auto-expire
after 3 days of inactivity. Use /clearnew to start a fresh session in the
current topic.

Forum topics: Enable "Topics" in your Telegram group settings. Each topic
becomes an independent Claude session, running in parallel.
"""

import asyncio
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable

# When run as `python bridge.py`, this module is loaded as `__main__`.
# Submodules under `patchbay/commands/` do `import bridge` to reach handler
# helpers via `bridge.X` (so test monkeypatches against `bridge.X` are
# visible). Without this alias, that import would re-execute bridge.py as a
# second module named `bridge`, and the re-entry would hit line 2148's
# `from patchbay.commands.lifecycle import cmd_cancel, ...` while lifecycle
# is mid-import → ImportError. Make `__main__` and `bridge` the same module.
sys.modules.setdefault("bridge", sys.modules[__name__])  # noqa: E402
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import telegramify_markdown  # noqa: E402
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.constants import ParseMode  # noqa: E402
from telegram.error import ChatMigrated, Forbidden, RetryAfter  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from patchbay.markdown_config import configure_telegramify  # noqa: E402

configure_telegramify()

# ---------------------------------------------------------------------------
# Import from package modules — these are the canonical implementations.
# Re-export at module level for backward compatibility with existing tests
# and validate.py.
# ---------------------------------------------------------------------------
from patchbay.config import (  # noqa: E402
    ACTIVITY_LOG,  # noqa: F401 — used by tests via bridge.ACTIVITY_LOG
    ANSI_RE,
    BOT_TOKEN,
    CHAT_PROJECTS_FILE,
    DEFAULT_HARNESS,
    FORGE_QUEUE_DIR,  # noqa: F401 — used by tests via bridge.FORGE_QUEUE_DIR
    HEARTBEAT_DELAY,
    HEARTBEAT_INTERVAL,
    MAX_QUEUED_MESSAGES,
    MAX_TIMEOUT,
    MAX_TURNS,
    MAX_WORKERS,
    PA_PLUGIN_DIR,
    PENDING_DIR,
    DOC_DIR,
    PHOTO_DIR,
    QUOTA_HIT_PREFIX,
    RESTART_DRAIN_TIMEOUT,
    RESTART_NOTIFY_FILE,
    SEND_RETRY_ATTEMPTS,
    SEND_RETRY_BASE_DELAY,
    SESSION_DIR,  # noqa: F401 — used by tests via bridge.SESSION_DIR
    SESSION_EXPIRY,
    SESSION_KEY_RE,
    SHUTDOWN_PROCESS_TIMEOUT,
    STALL_POLL_INTERVAL,
    STALL_TIMEOUT,
    TELEGRAM_MSG_LIMIT,
    TYPING_INTERVAL,
    USAGE_WEEKLY_TOKEN_CAP,
    VALID_HARNESSES,
    WORKING_DIR,
    logger,
)
from patchbay.sessions import (  # noqa: E402
    PENDING_MAX_ATTEMPTS,
    _sanitize_session_key,  # noqa: F401 — used by tests via bridge._sanitize_session_key
    _session_key,
    archive_failed_pending,
    bump_pending_attempts,
    clear_pending,
    clear_session,
    consume_stall_kill,
    get_session_id,
    mark_stall_kill,
    save_pending,
    save_session_id,
)
from patchbay.harness import (  # noqa: E402
    CAPABILITIES_BY_NAME,
    PiHarness,
    ToolUse,  # noqa: F401 — re-exported for tests
    TurnError,
    TurnFinal,
    TurnRequest,
)
from patchbay.parser import (  # noqa: E402
    _parse_events,  # noqa: F401 — re-exported for validate.py smoke tests
    is_empty_success_response,
    parse_claude_response,
)
from patchbay.quota import (  # noqa: E402
    handoff_to_forge as _handoff_to_forge_impl,
    is_quota_error as _is_quota_error_impl,
)
from patchbay.activity import log_activity  # noqa: E402
from patchbay.file_send import extract_file_sentinels, send_files  # noqa: E402
from patchbay.outbound import get_recent_outbound, log_outbound_response  # noqa: E402
from patchbay.text_split import (  # noqa: E402
    is_markdownv2_balanced,
    split_for_telegram,
)
from patchbay.models import (  # noqa: E402
    DEFAULT_MODEL,
    VALID_MODELS,
    extract_model_prefix,
    get_chat_model,
    resolve_model,
    set_chat_model,
)
from patchbay.efforts import (  # noqa: E402
    DEFAULT_EFFORT,
    VALID_EFFORTS,
    get_chat_effort,
    resolve_effort,
    set_chat_effort,
)
from patchbay.projects import (  # noqa: E402
    _load_chat_projects,
    _parse_project_entry,
    get_all_projects as _get_all_projects,
    get_chat_agent,
    get_chat_harness,
    get_chat_heartbeat,
    get_chat_title,
    get_chat_working_dir,
    set_chat_harness,
    set_chat_project,
    set_chat_title,
)

# Backward-compatible names for functions that were renamed
_is_quota_error = _is_quota_error_impl
_handoff_to_forge = _handoff_to_forge_impl
_log_activity = log_activity

# Module-level _ANSI_RE for backward compat
_ANSI_RE = ANSI_RE
_SESSION_KEY_RE = SESSION_KEY_RE

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# ---------------------------------------------------------------------------
# Per-session state
#
# Consolidates what used to be six parallel dicts keyed by session_key into a
# single SessionState object. The legacy module-level dicts below remain as
# the live data store during the migration; each subsequent commit will move
# one field from the legacy dicts onto SessionState until they can all be
# deleted. New code should read/write through `_get_session_state(key)`.
# ---------------------------------------------------------------------------


class QueuedMessage(NamedTuple):
    """A debounced queued message awaiting processing.

    Carries `pending_id` so the corresponding pending file (saved at queue
    time) can be cleared after the queued batch delivers, and so that
    SIGTERM-tearing-the-loop-down before drain leaves the file in place
    for replay_pending() on the next bridge start.
    """

    text: str
    pending_id: str


@dataclass
class SessionState:
    """All per-session runtime state, keyed by session_key in `_sessions`."""

    proc: subprocess.Popen | None = None  # subprocess harnesses (pi) only; mirrored by harness's proc_setter
    harness: object | None = None  # whichever harness instance is driving the active turn
    worker_loop: asyncio.AbstractEventLoop | None = (
        None  # loop owned by _drive_harness_sync; needed for cross-loop cancel of SDK-based harnesses
    )
    started_at: float | None = None  # time.time() when processing began
    last_event_at: float | None = None  # last time stall-detector saw activity
    queue: list[QueuedMessage] = field(default_factory=list)  # debounced messages awaiting processing
    processing: bool = False  # True while a claude run is in flight for this key
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


# Owned by patchbay.runtime so command handlers (in patchbay/commands/) can
# import the same dict without a circular dependency on bridge. The local
# alias keeps existing call sites unchanged.
from patchbay.runtime import sessions as _sessions  # noqa: E402


def _get_session_state(key: str) -> SessionState:
    """Return the SessionState for `key`, creating an empty one if needed."""
    state = _sessions.get(key)
    if state is None:
        state = SessionState()
        _sessions[key] = state
    return state


def _iter_active_procs() -> list[tuple[str, subprocess.Popen]]:
    """Snapshot of (session_key, proc) for every session with a live subprocess.

    Subprocess harnesses only (pi). Use `_iter_active_sessions` for the
    view used by /ping, stall detector.
    """
    return [(k, s.proc) for k, s in _sessions.items() if s.proc is not None]


def _iter_active_sessions() -> list[tuple[str, "SessionState"]]:
    """Snapshot of (session_key, state) for every session running a turn,
    regardless of harness. Used by /ping, the stall detector, /restart,
    and graceful shutdown so SDK turns are visible too.

    A session counts as active when either `state.proc` is set
    (subprocess harnesses) or `state.harness` is set (SDK harnesses).
    Either signal alone is sufficient.
    """
    return [(k, s) for k, s in _sessions.items() if s.proc is not None or s.harness is not None]


async def _interrupt_session_async(state: "SessionState") -> None:
    """Soft interrupt — SIGINT for the pi subprocess.

    Gives pi a chance to finish cleanly. Use /kill for hard termination.
    """
    import signal as _signal

    proc = state.proc
    if proc is not None:
        try:
            proc.send_signal(_signal.SIGINT)
        except OSError:
            pass


async def _cancel_session_async(state: "SessionState") -> None:
    """Hard cancel — SIGKILL the pi subprocess.

    `state.proc` holds the Popen handle; SIGKILL via `proc.kill()`.
    Sync, immediate; the harness's drain returns and
    `_drive_harness_sync` exits naturally.

    Idempotent and silent on error — callers (cmd_kill, stall detector,
    shutdown) treat this as a fire-and-forget request.
    """
    proc = state.proc
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass


async def _claim_or_queue(state: SessionState, text: str, pending_id: str = "") -> tuple[str, int | None]:
    """Atomically claim the processing lane or enqueue the message.

    Returns one of:
      ("claimed", None) — caller now owns processing for this session.
      ("queued", depth) — caller's message has been queued at the given depth.
      ("full",   None) — queue is full; caller should drop the message.

    Holding state.lock around the check+claim+enqueue closes a debounce
    race where two messages arriving in the same event-loop tick could
    both pass the `if state.processing` check and stomp on each other.

    `pending_id` is the on-disk pending-file id for this message, threaded
    in so queued messages survive a SIGTERM-mid-drain — replay_pending()
    on the next bridge start re-runs anything still on disk.
    """
    async with state.lock:
        if not state.processing:
            state.processing = True
            state.started_at = time.time()
            return ("claimed", None)
        if len(state.queue) >= MAX_QUEUED_MESSAGES:
            return ("full", None)
        state.queue.append(QueuedMessage(text=text, pending_id=pending_id))
        return ("queued", len(state.queue))


async def _drain_next(state: SessionState) -> list[QueuedMessage] | None:
    """Pop the next batch of queued messages, or release processing.

    Returns the batch if there are queued messages. Otherwise atomically
    clears the processing flag (and started_at) under the lock and returns
    None — that's the only safe place to release ownership, since a
    concurrent _claim_or_queue would otherwise see processing=True, queue
    a message, then watch it get orphaned when we cleared the flag.
    """
    async with state.lock:
        if not state.queue:
            state.processing = False
            state.started_at = None
            return None
        batch = state.queue
        state.queue = []
        return batch


async def _release_processing(state: SessionState) -> None:
    """Force-clear the processing flag. Used as defensive cleanup on an
    exception path — _drain_next normally handles the happy path."""
    async with state.lock:
        state.processing = False
        state.started_at = None


# Track remote-control process (only one at a time, keyed by session key)
_remote_proc: subprocess.Popen | None = None
_remote_proc_key: str | None = None
_remote_drain_thread: object | None = None  # threading.Thread when active


def _start_remote_drain(proc: subprocess.Popen) -> None:
    """Drain proc.stdout in a daemon thread for the rest of its life.

    Without this, `claude remote-control` can deadlock on a full stdout
    pipe (~64KB on macOS) once the initial-output capture window closes
    and nobody is reading anymore. The drain just discards lines —
    everything the user needs (connection info, etc.) was already
    captured during the initial 10s window. Audit §17.
    """
    import threading

    def _drain() -> None:
        try:
            for _ in iter(proc.stdout.readline, ""):
                pass
        except (OSError, ValueError):
            pass
        finally:
            try:
                proc.stdout.close()
            except OSError:
                pass

    global _remote_drain_thread
    t = threading.Thread(target=_drain, name="remote-control-drain", daemon=True)
    t.start()
    _remote_drain_thread = t


# Message debounce: batch messages that arrive while Claude is processing


# Flag to block new messages during graceful shutdown
_shutting_down = False

# Bot instance (set in post_init)
_bot_instance = None



# Captured once at runtime module import time — used by /health to report
# process uptime. Local alias preserves existing _BRIDGE_STARTED_AT call sites.
from patchbay.runtime import BRIDGE_STARTED_AT as _BRIDGE_STARTED_AT  # noqa: E402


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def _drive_harness_sync(
    harness,
    req: TurnRequest,
    state: "SessionState | None" = None,
) -> list:
    """Drive the harness async iterator from a sync context, return all events.

    `run_claude` is sync (called via `loop.run_in_executor` by the orchestrator),
    but the harness exposes an async iterator. This helper bridges the two:
    we run a fresh event loop on the worker thread, drain the iterator, and
    hand back the collected `TurnEvent` list. One loop per call is fine —
    harness work is dominated by the subprocess wait, not loop overhead.

    When `state` is given, we mirror the harness instance and the
    worker-thread's event loop into it for the duration of the turn.
    `_cancel_session_async` uses `state.proc.kill()` for cancellation.
    The fields are cleared in `finally`.
    """
    events: list = []

    async def _drive() -> None:
        if state is not None:
            state.worker_loop = asyncio.get_running_loop()
            state.harness = harness
        try:
            async for event in harness.run_turn(req):
                events.append(event)
        finally:
            if state is not None:
                state.harness = None
                state.worker_loop = None

    asyncio.run(_drive())
    return events


def run_claude(
    message: str,
    session_key: str,
    _retry: bool = False,
    model: str | None = None,
    max_turns_override: int | None = None,
) -> str:
    """Invoke a coding-agent harness for one turn, translate events to a string.

    max_turns_override: when set, replaces MAX_TURNS for this invocation
    only. Used by the OOM self-heal path (see patchbay/self_heal.py) to
    retry with a tighter turn budget after a kill.
    """
    session_id = get_session_id(session_key)
    chat_cwd = get_chat_working_dir(session_key)

    projects = _load_chat_projects()
    entry = projects.get(session_key)
    rel_path, _ = _parse_project_entry(entry)
    project_info = f"~/Developer/{rel_path}" if rel_path else "~/Developer (default)"

    agent_name = get_chat_agent(session_key)

    system_prompt = (
        "The user is messaging you via Telegram from their phone. "
        "You have full access to all your MCP tools and can do real work. "
        "For email access, use the himalaya CLI: 'himalaya envelope list --account icloud' or '--account gmail' to list emails, "
        "'himalaya message read <id> --account <account>' to read them. "
        "IMPORTANT: NEVER use the AskUserQuestion tool - it requires interactive terminal UI that doesn't work through Telegram. "
        "Instead, ask questions as plain text in your response and let the user reply naturally.\n\n"
        "VOICE — replies are for a phone screen, not a desk:\n"
        "1. Lead with substance. No opener ('I'll do X', 'Now let me Y') and no closer ('Want me to...', 'Let me know...').\n"
        "2. Prefer lists over prose when itemizing. Use NUMBERS for ordered/sequential items, LETTERS (a, b, c) for unordered items the user might reference back. Avoid plain bullets (•/-) — the user can't cite them shorthand.\n"
        "3. Each list item is one line with at most one parenthetical for crucial detail. No nested lists.\n"
        "4. Plain-English noun phrases. No class names, SDK type names, or `module.symbol` paths unless that's literally what's being changed.\n"
        "5. Flag inactions explicitly: 'Did NOT X — <one short reason>'. The user needs to know what's still on their plate.\n"
        "6. Tests as raw counts only ('17 new, 692 total'). No narrative around them.\n"
        "7. Length: roughly 60-80 words for multi-item status updates, 10-30 words for single facts. When uncertain, cut.\n"
        "8. The user complains 'too much' when answers are long, never when short.\n\n"
        f"TURN LIMIT: This session has a {MAX_TURNS}-turn limit. If a task will take more than ~20 tool calls, "
        "decompose it: do the critical/unblocking work now, create beads for the remaining subtasks, "
        "then report what you did and what's queued. Don't get cut off mid-task.\n\n"
        "CRITICAL: Your FINAL output MUST be a text response to the user — never end on a tool call. "
        "If you've done work via tools, summarize what you did in a short text message at the end. "
        "Even one sentence is acceptable; total silence is the only true failure. "
        "If you don't produce text, the bridge has to burn an extra Claude query asking you to "
        "summarize, and the user sees '(Completed N turns…)' until that retry returns. "
        "Bottom line: always close the conversation with at least a brief text reply.\n\n"
        "TOOL STDOUT IS INVISIBLE TO THE USER: Anything printed by Bash, Python scripts, or other tools "
        "goes to YOUR context only — never to Telegram. If you run a script that prints prototypes, "
        "tables, mockups, or any content you want the user to see, you MUST inline that content verbatim "
        "in your text response. Never write 'three shapes above', 'see output', 'here's the result', "
        "or any phrase that implies the user can see what the tool printed. If the tool output is what "
        "you're showing them, paste it into your text message. This failure mode has bitten us before — "
        "when in doubt, inline it.\n\n"
        "FORMATTING: Telegram renders your replies as MarkdownV2 (converted from standard markdown by the bridge). "
        "Use normal markdown — `inline code`, ```code blocks```, **bold**, *italic*, and block quotes all render. "
        "Tables are NOT supported by Telegram and will be rendered as a plain code block, so prefer numbered/lettered "
        "lists (per the VOICE rules above) or ASCII-aligned columns inside a ``` code block for tabular data.\n\n"
        "HEADLESS — You and the user are not co-located. The user is on a phone via Telegram; "
        "you are on a remote machine with no GUI session, no display, no one to click dialogs or "
        "watch a screen. Don't depend on interactive computer use — yours or the user's. Reason from "
        "code, tests, and static analysis. If a task genuinely needs a runtime/GUI step (rare), stop "
        "and say so rather than asking the user to do it on their phone.\n\n"
        "SHARED FILES: There is a ProtonDrive folder synced to this machine. "
        "Find it at ~/Library/CloudStorage/ProtonDrive-*/Claude-Support (glob for the exact path). "
        "You can drop files there (documents, images, exports) for the user to access from any device. "
        "Photos sent from Telegram are already handled separately via the photo handler.\n\n"
        "SENDING FILES TO TELEGRAM: To attach a file directly to your reply (image, gpx, pdf, "
        "anything), include a sentinel anywhere in your response text:\n"
        "  [[send-file: /absolute/path/to/file.ext]]\n"
        "  [[send-file: /absolute/path/to/photo.png | optional caption]]\n"
        "Image MIMEs are sent inline via sendPhoto; everything else as a document. "
        "Path must be absolute. Photos cap at 10MB, documents at 50MB. "
        "The sentinel itself is stripped from the message — write it on its own line. "
        "Use this instead of dropping into ProtonDrive when the user wants the file in-chat.\n\n"
        f"Telegram session key: {session_key}\n"
        f"Working directory: {project_info}\n"
        f"Chat projects config: {CHAT_PROJECTS_FILE}\n"
        "You can change your own project directory by editing chat_projects.json "
        "(map session key to a path relative to ~/Developer). "
        "After changing it, tell the user to run /clearnew to pick up the new cwd."
    )

    # Inject recent outbound notifications so the session knows what was sent
    recent = get_recent_outbound(session_key, max_age=86400.0)
    if recent:
        lines = []
        for entry in recent[-3:]:  # last 3 messages max
            ts = datetime.fromtimestamp(entry["ts"]).strftime("%H:%M")
            src = entry.get("source", "?")
            txt = entry["text"][:500]
            lines.append(f"  [{ts}] ({src}): {txt}")
        system_prompt += (
            "\n\nRECENT NOTIFICATIONS sent to this thread (the user may be replying to one of these):\n"
            + "\n".join(lines)
        )

    if agent_name:
        system_prompt += (
            f"\n\nAGENT MODE: You are the '{agent_name}' agent from Fanta. "
            f"CLAUDE.md has the full loading order — follow it. "
            f"Do NOT read other agents' files. You are ONLY the {agent_name} agent. "
            f"Skip MEMORY.md in Telegram context (per Fanta conventions)."
        )

    # If the previous run was killed by the stall detector, warn this run
    # against repeating whatever headless-unsafe command likely hung it.
    stall_info = consume_stall_kill(session_key)
    if stall_info:
        idle_min = stall_info.get("idle_minutes", 0)
        system_prompt += (
            f"\n\nPRIOR RUN KILLED: Your previous invocation in this session was terminated by the "
            f"stall detector after {idle_min:.0f} minutes of zero CPU with no output. The most likely "
            "cause is that you ran a command requiring a macOS TCC/GUI permission dialog (XCUITest, "
            "`xcodebuild test` with UI tests, `osascript` targeting a GUI app you hadn't pre-approved, "
            "Simulator boot, Instruments, etc.). The user is on their phone — they cannot click the dialog. "
            "Look at your last tool call in the conversation history, DO NOT RETRY IT, and pick a "
            "headless-safe alternative (swift test, tuist build, unit tests, static analysis)."
        )

    # Resolve model / effort the same way the legacy path did.
    if not model:
        model = resolve_model(session_key)
    effort = resolve_effort(session_key)

    # Resolve harness: per-chat override > DEFAULT_HARNESS env. Every
    # supported harness is dispatched directly — selection is logged on
    # every activity entry under `harness=` and `harness_requested=` so
    # cross-harness comparison stays possible.
    harness_name = get_chat_harness(session_key) or DEFAULT_HARNESS
    if harness_name not in VALID_HARNESSES:
        logger.warning(
            "Unknown harness %r for %s; falling back to %s",
            harness_name,
            session_key,
            DEFAULT_HARNESS,
        )
        harness_name = DEFAULT_HARNESS
    effective_harness = harness_name

    if session_id:
        logger.info("Resuming session %s for %s", session_id[:12], session_key)
    logger.info(
        "Launching claude in %s for %s (model=%s, effort=%s, harness=%s)",
        chat_cwd,
        session_key,
        model or "default",
        effort,
        effective_harness,
    )
    invoke_start = time.time()
    _log_activity(
        "turn_invoke",
        session_key=session_key,
        cwd=chat_cwd,
        model=model or "default",
        effort=effort,
        resume=bool(session_id),
        harness=effective_harness,
        harness_requested=harness_name,
    )

    state = _get_session_state(session_key)
    state.last_event_at = time.time()

    def _on_progress() -> None:
        st = _sessions.get(session_key)
        if st is not None:
            st.last_event_at = time.time()

    def _proc_setter(proc: subprocess.Popen | None) -> None:
        st = _sessions.get(session_key)
        if st is not None:
            st.proc = proc

    # Pi (badlogicgames/pi) — multi-model coding agent. Uses its own
    # session storage (~/.pi/agent/sessions). Subprocess-based, so
    # proc_setter mirrors into state.proc for /kill / stall.
    harness = PiHarness(
        max_timeout_seconds=MAX_TIMEOUT,
        on_progress=_on_progress,
        proc_setter=_proc_setter,
    )
    req = TurnRequest(
        prompt=message,
        session_key=session_key,
        project_dir=Path(chat_cwd),
        system_prompt=system_prompt,
        resume_session_id=session_id,
        model=model,
        effort=effort,
        allowed_tools=None,
        disallowed_tools=["AskUserQuestion", "EnterPlanMode", "ExitPlanMode"],
        max_turns=(max_turns_override if max_turns_override is not None else MAX_TURNS),
        plugin_dir=PA_PLUGIN_DIR,
        extra=None,
    )

    try:
        events = _drive_harness_sync(harness, req, state)
    except (asyncio.CancelledError, KeyboardInterrupt):
        # /kill cancels the in-flight task; CancelledError unwinds out of
        # asyncio.run() in _drive_harness_sync. Log a terminal activity event
        # so /soak and the activity log see the kill — without this branch
        # the turn shows only `turn_invoke` + `process_kill` and looks
        # indistinguishable from a wedge. cmd_kill already replied to the
        # user and cleared state.processing; we re-raise so the orchestrator
        # skips its own send.
        duration = time.time() - invoke_start
        _log_activity(
            "turn_cancelled",
            session_key=session_key,
            duration=duration,
            elapsed_ms=int(duration * 1000),
            harness=effective_harness,
        )
        raise
    finally:
        st = _sessions.get(session_key)
        if st is not None:
            st.proc = None
            st.last_event_at = None
            # Belt-and-suspenders: _drive_harness_sync clears these in its
            # own finally too, but a hard exception out of asyncio.run could
            # in principle leave them stale.
            st.harness = None
            st.worker_loop = None

    duration = time.time() - invoke_start
    final = events[-1] if events else None

    # Failure path — TurnError. Each kind maps to an existing recovery branch.
    if isinstance(final, TurnError):
        if final.kind == "timeout":
            _log_activity(
                "turn_timeout",
                session_key=session_key,
                duration=duration,
                elapsed_ms=int(duration * 1000),
                harness=effective_harness,
            )
            return f"[Timed out after {MAX_TIMEOUT // 60} min] Session preserved — send your message again to resume."

        if final.kind == "corrupt_session" and not _retry:
            stale = final.metadata.get("stale_session_id") or session_id
            logger.warning(
                "Stale session %s for %s, retrying fresh",
                (stale or "?")[:12],
                session_key,
            )
            clear_session(session_key)
            return run_claude(message, session_key, _retry=True)

        if final.kind == "oom" and not _retry:
            from patchbay.self_heal import (
                OOM_RETRY_MAX_TURNS,
                OOM_RETRY_PROMPT_TRIM,
                dispatch_repair,
            )

            rc = final.metadata.get("exit_code", 0)
            result = dispatch_repair(
                "claude_oom_137",
                {"session_key": session_key, "returncode": rc},
            )
            if result.fixed:
                logger.warning(
                    "OOM kill (rc=%d) for %s, retrying with max_turns=%d trimmed_prompt=%dch",
                    rc,
                    session_key,
                    OOM_RETRY_MAX_TURNS,
                    OOM_RETRY_PROMPT_TRIM,
                )
                return run_claude(
                    message[:OOM_RETRY_PROMPT_TRIM],
                    session_key,
                    _retry=True,
                    model=model,
                    max_turns_override=OOM_RETRY_MAX_TURNS,
                )

        if final.kind == "rate_limit":
            stderr = final.metadata.get("stderr", "")
            source = "stderr" if stderr else "events+stderr"
            if stderr:
                logger.warning(
                    "Quota/rate limit detected (no output) for %s: %s",
                    session_key,
                    stderr[:200],
                )
            else:
                logger.warning("Quota/rate limit detected for %s", session_key)
            _log_activity(
                "quota_hit",
                session_key=session_key,
                duration=duration,
                source=source,
                harness=effective_harness,
            )
            return QUOTA_HIT_PREFIX + message

        if final.kind == "max_turns":
            sess_id = final.metadata.get("session_id")
            if sess_id:
                save_session_id(session_key, sess_id)
            num_turns = final.metadata.get("num_turns")
            _log_activity(
                "turn_complete",
                session_key=session_key,
                duration=duration,
                elapsed_ms=int(duration * 1000),
                turns_used=num_turns,
                exit_code=0,
                response_len=len(final.message),
                harness=effective_harness,
            )
            return final.message

        # unknown / process_died — surface what we have.
        rc = final.metadata.get("exit_code", 0)
        stderr = final.metadata.get("stderr", "")
        if rc and stderr:
            logger.warning(
                "Claude exited %d for %s. stderr: %s",
                rc,
                session_key,
                stderr[:300],
            )
        _log_activity(
            "turn_error",
            session_key=session_key,
            duration=duration,
            elapsed_ms=int(duration * 1000),
            error=(stderr[:200] if stderr else final.message[:200]) or "no output",
            harness=effective_harness,
        )
        return final.message or "(no output)"

    # Success path — TurnFinal with the aggregated text.
    if isinstance(final, TurnFinal):
        if final.session_id:
            save_session_id(session_key, final.session_id)
            logger.info("Saved session %s for %s", final.session_id[:12], session_key)
        _log_activity(
            "turn_complete",
            session_key=session_key,
            duration=duration,
            elapsed_ms=int(duration * 1000),
            turns_used=final.num_turns,
            exit_code=0,
            response_len=len(final.raw_text),
            harness=effective_harness,
        )
        response = final.raw_text or "(no parseable response)"

        return response

    # Defensive: harness didn't terminate properly. Treat as no output.
    _log_activity(
        "turn_error",
        session_key=session_key,
        duration=duration,
        elapsed_ms=int(duration * 1000),
        error="harness returned no terminator",
        harness=effective_harness,
    )
    return "(no output)"


# ---------------------------------------------------------------------------
# Telegram helpers — moved to patchbay/telegram_send.py
# (keep_typing, _to_markdownv2, _send_response, _notify_delivery_failure are
# re-exported near the bottom of this module for test-patch compatibility.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Pending message replay
# ---------------------------------------------------------------------------


async def replay_pending(bot) -> None:
    """Replay messages that were lost when the bridge was killed mid-processing."""
    pending_files = sorted(PENDING_DIR.glob("*.json"))
    if not pending_files:
        return

    logger.info("Found %d pending messages to replay", len(pending_files))

    for f in pending_files:
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, KeyError):
            f.unlink()
            continue

        if not all(k in data for k in ("chat_id", "session_key", "text", "timestamp")):
            logger.warning("Skipping malformed pending file %s", f.name)
            f.unlink(missing_ok=True)
            continue

        if time.time() - data["timestamp"] > SESSION_EXPIRY:
            f.unlink(missing_ok=True)
            logger.info("Discarding expired pending message %s", f.stem)
            continue

        chat_id = data["chat_id"]
        thread_id = data.get("thread_id")
        session_key = data["session_key"]
        text = data["text"]

        # Bump attempt count BEFORE processing. After PENDING_MAX_ATTEMPTS
        # failed retries, archive the file and tell the user we gave up
        # rather than risking another crash loop.
        attempts = bump_pending_attempts(f)
        if attempts > PENDING_MAX_ATTEMPTS:
            logger.warning(
                "Giving up on pending %s after %d attempts; archiving",
                f.stem,
                attempts - 1,
            )
            archive_failed_pending(f)
            send_kwargs = {"chat_id": chat_id}
            if thread_id is not None:
                send_kwargs["message_thread_id"] = thread_id
            try:
                await bot.send_message(
                    text=(
                        f"[Tried {PENDING_MAX_ATTEMPTS}× to replay your message after bridge restart; "
                        f"giving up. Original: {text[:200]}]"
                    ),
                    **send_kwargs,
                )
            except Exception as exc:
                logger.error("Failed to notify give-up for %s: %s", f.stem, exc)
            continue

        logger.info(
            "Replaying message for %s (attempt %d/%d): %s",
            session_key,
            attempts,
            PENDING_MAX_ATTEMPTS,
            text[:80],
        )

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(keep_typing(chat_id, thread_id, stop_typing, bot))

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(_executor, run_claude, text, session_key)
        except Exception as e:
            # Leave the pending file in place — the bumped attempt count
            # persists, so the next bridge start will retry until the cap.
            logger.error("Error replaying %s (attempt %d): %s", f.stem, attempts, e)
            stop_typing.set()
            await typing_task
            continue
        else:
            stop_typing.set()
            await typing_task

        full_response = "[Recovered after bridge restart]\n\n" + response

        send_kwargs = {"chat_id": chat_id}
        if thread_id is not None:
            send_kwargs["message_thread_id"] = thread_id

        try:
            for i in range(0, len(full_response), TELEGRAM_MSG_LIMIT):
                await bot.send_message(text=full_response[i : i + TELEGRAM_MSG_LIMIT], **send_kwargs)
        except Exception as exc:
            # Delivery failed — leave the file with its bumped attempt count
            # so the next bridge start can retry until we hit the cap.
            logger.error("Failed to deliver replayed response for %s: %s", f.stem, exc)
            continue

        # Success: drop the pending record.
        f.unlink(missing_ok=True)
        logger.info("Replayed pending message %s", f.stem)


# ---------------------------------------------------------------------------
# Message and photo handlers
# ---------------------------------------------------------------------------


def _maybe_handoff_quota(response: str, session_key: str, chat_id: int, thread_id: int | None) -> str:
    """If `response` is a quota-hit sentinel, hand the original message off
    to Forge and return a user-facing replacement. Otherwise return the
    response unchanged. Only message-shaped turns invoke this; photo /
    document handlers don't because their prompts include local file paths
    a Forge worker can't reach."""
    if not response.startswith(QUOTA_HIT_PREFIX):
        return response
    original_msg = response[len(QUOTA_HIT_PREFIX) :]
    session_id = get_session_id(session_key)
    chat_cwd = get_chat_working_dir(session_key)
    handed_off = _handoff_to_forge(
        session_key=session_key,
        message=original_msg,
        chat_id=chat_id,
        thread_id=thread_id,
        session_id=session_id,
        working_dir=chat_cwd,
    )
    if handed_off:
        return (
            "Hit a quota/rate limit. Handed this off to Forge — "
            "it'll pick up where this left off and send the response "
            "back here when done."
        )
    return "Hit a quota/rate limit. Tried to hand off to Forge but failed to write the queue file. Try again later."


async def _process_with_claude_turn(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    session_key: str,
    chat_id: int,
    thread_id: int | None,
    prompt: str,
    label: str,  # "message" / "photo" / "document" — drives log-event names + UX strings
    drop_message: str,  # what to show on "queue full"
    queued_message: str,  # what to show on "queued"
    model: str | None = None,  # per-message override (only "message" uses this today)
    quota_handoff: bool = False,  # only "message" uses Forge handoff
    on_drop: Callable[[], None] | None = None,  # called when queue is full (file cleanup)
    on_finish: Callable[[], None] | None = None,  # called in finally after processing
) -> None:
    """Shared lifecycle for one user→claude turn: claim the lane, run the
    main invocation, send the reply, drain the queued follow-ups, release.

    The three handlers (handle_message, handle_photo, handle_document) used
    to inline ~100 lines of this each. Pulling them onto one function means
    a bug in queueing or drain ordering is one fix instead of three. The
    per-handler pieces (extracting the prompt, downloading attachments,
    file cleanup) stay in the handlers; this function takes the resulting
    `prompt` and runs it.
    """
    state = _get_session_state(session_key)
    # Save pending FIRST so even queued messages (which sit in an in-memory
    # queue until drain) survive a SIGTERM-mid-debounce — replay_pending()
    # picks up anything still on disk on the next bridge start.
    pending_id = save_pending(chat_id, thread_id, prompt, session_key)

    status, depth = await _claim_or_queue(state, prompt, pending_id)

    if status == "full":
        # Queue full — drop the message AND its pending file (we've told the
        # user we won't be processing it; replaying after restart would be
        # confusing).
        clear_pending(pending_id)
        await update.message.reply_text(drop_message)
        logger.warning("Queue full for %s, dropping %s", session_key, label)
        _log_activity(f"{label}_dropped", session_key=session_key, depth=MAX_QUEUED_MESSAGES)
        if on_drop is not None:
            on_drop()
        return

    if status == "queued":
        # Pending file stays on disk; the drain loop in the active turn
        # will clear it after delivery, or replay_pending() will recover
        # it if the bridge dies before drain reaches it.
        await update.message.reply_text(queued_message.format(depth=depth))
        logger.info("Queued %s for %s (depth: %d)", label, session_key, depth)
        _log_activity(f"{label}_queued", session_key=session_key, depth=depth)
        return

    # status == "claimed" — we own this session's processing lane until _drain_next clears it.
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, thread_id, stop_typing, context.bot))

    # Feature 1: heartbeat bubble — send ⏳ Working — N min after HEARTBEAT_DELAY
    # seconds if the turn is still in flight. msg_holder accumulates the sent
    # message_id so the main path can delete the bubble on success (Feature 2).
    heartbeat_holder: list[int] = []
    heartbeat_task: asyncio.Task | None = None
    if get_chat_heartbeat(session_key):
        heartbeat_task = asyncio.create_task(
            _run_heartbeat(
                chat_id,
                thread_id,
                stop_typing,
                context.bot,
                time.time(),
                heartbeat_holder,
                delay=float(HEARTBEAT_DELAY),
                interval=float(HEARTBEAT_INTERVAL),
            )
        )

    try:
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(_executor, lambda: run_claude(prompt, session_key, model=model))
        except Exception as e:
            logger.error("Error running claude for %s %s: %s", label, session_key, e)
            response = f"Error: {e}"

        if quota_handoff:
            response = _maybe_handoff_quota(response, session_key, chat_id, thread_id)

        # Delivered flag pattern: only clear the pending file after we've
        # confirmed the send returned without raising. CancelledError (SIGTERM
        # tearing down the loop mid-await) is a BaseException, not Exception,
        # so it skips the except block and propagates — but the finally
        # below still runs with delivered=False, preserving the pending file
        # for replay_pending() to recover on the next bridge start.
        delivered = False
        try:
            await _send_response(context.bot, chat_id, thread_id, response)
            delivered = True
            # Feature 2: delete heartbeat bubble on successful delivery
            if heartbeat_holder:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=heartbeat_holder[0])
                    heartbeat_holder.clear()
                except Exception:
                    pass
        except Exception as e:
            logger.error("Failed to send %s response for %s: %s", label, session_key, e)
            await _notify_delivery_failure(context.bot, chat_id, thread_id, session_key)
        finally:
            if delivered:
                clear_pending(pending_id)

        # Drain queued messages: each pop from _drain_next either yields a
        # batch or atomically clears state.processing and ends the loop.
        while True:
            batch = await _drain_next(state)
            if batch is None:
                break
            logger.info("Processing %d queued message(s) for %s", len(batch), session_key)
            combined = (
                batch[0].text
                if len(batch) == 1
                else "\n\n---\n\n".join(f"[Follow-up {i + 1}]\n{item.text}" for i, item in enumerate(batch))
            )
            try:
                response = await loop.run_in_executor(_executor, run_claude, combined, session_key)
            except Exception as e:
                logger.error("Error running claude for queued batch %s: %s", session_key, e)
                response = f"Error: {e}"
            batch_delivered = False
            try:
                await _send_response(context.bot, chat_id, thread_id, response)
                batch_delivered = True
                if heartbeat_holder:
                    try:
                        await context.bot.delete_message(chat_id=chat_id, message_id=heartbeat_holder[0])
                        heartbeat_holder.clear()
                    except Exception:
                        pass
            except Exception as e:
                logger.error("Failed to send queued response for %s: %s", session_key, e)
                await _notify_delivery_failure(context.bot, chat_id, thread_id, session_key)
            finally:
                if batch_delivered:
                    for item in batch:
                        clear_pending(item.pending_id)

    except Exception:
        # Defensive: ensure processing flag is cleared on any uncaught
        # exception escaping the drain loop.
        await _release_processing(state)
        raise
    finally:
        stop_typing.set()
        await typing_task
        if heartbeat_task is not None and not heartbeat_task.done():
            heartbeat_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
        if on_finish is not None:
            on_finish()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _shutting_down:
        await update.message.reply_text("Bridge is shutting down. Message not processed — please resend in a moment.")
        return

    text = update.message.text
    if not text:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)
    logger.info("From %d [%s]: %s", user_id, key, text[:80])
    _log_activity("message", user_id=user_id, session_key=key, text_len=len(text))

    msg_model, clean_text = extract_model_prefix(text)

    # Feature 5: inject quoted text when replying to a bot message
    reply_msg = update.message.reply_to_message
    if reply_msg is not None:
        from patchbay.reply_store import lookup as _rs_lookup

        quoted = _rs_lookup(chat_id, reply_msg.message_id)
        if quoted:
            clean_text = f"[Replying to: {quoted[:500]}]\n\n{clean_text}"

    await _process_with_claude_turn(
        update,
        context,
        session_key=key,
        chat_id=chat_id,
        thread_id=thread_id,
        prompt=clean_text,
        label="message",
        drop_message=(f"Queue full ({MAX_QUEUED_MESSAGES}) — message dropped. Wait for current response to finish."),
        queued_message="Queued ({depth}) — will send when current response finishes.",
        model=msg_model,
        quota_handoff=True,
    )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _shutting_down:
        await update.message.reply_text("Bridge is shutting down. Photo not processed — please resend in a moment.")
        return

    photo = update.message.photo[-1]  # highest resolution
    caption = update.message.caption or "Describe this image."

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    user_id = update.effective_user.id
    tg_file = await context.bot.get_file(photo.file_id)
    local_path = PHOTO_DIR / f"{photo.file_unique_id}.jpg"
    await tg_file.download_to_drive(local_path)
    logger.info("Downloaded photo to %s for %s", local_path, key)
    _log_activity("photo", user_id=user_id, session_key=key, caption_len=len(caption))

    prompt = f"{caption}\n\n[An image has been saved to {local_path} — use the Read tool to view it before responding.]"

    def _cleanup() -> None:
        local_path.unlink(missing_ok=True)

    await _process_with_claude_turn(
        update,
        context,
        session_key=key,
        chat_id=chat_id,
        thread_id=thread_id,
        prompt=prompt,
        label="photo",
        drop_message=(f"Queue full ({MAX_QUEUED_MESSAGES}) — photo dropped. Wait for current response to finish."),
        queued_message="Photo queued ({depth}) — will send when current response finishes.",
        on_drop=_cleanup,
        on_finish=_cleanup,
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle file attachments (documents) sent via Telegram."""
    if _shutting_down:
        await update.message.reply_text("Bridge is shutting down. File not processed — please resend in a moment.")
        return

    doc = update.message.document
    if not doc:
        return

    caption = update.message.caption or ""
    file_name = doc.file_name or f"file_{doc.file_unique_id}"

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)
    user_id = update.effective_user.id

    tg_file = await context.bot.get_file(doc.file_id)
    local_path = DOC_DIR / file_name
    await tg_file.download_to_drive(local_path)
    logger.info("Downloaded document %s to %s for %s", file_name, local_path, key)
    _log_activity("document", user_id=user_id, session_key=key, caption_len=len(caption))

    prompt = (
        f"{caption}\n\n"
        f"[A file has been saved to {local_path} — "
        f"use the Read tool or Bash tool to inspect it as appropriate.]"
    )

    def _cleanup() -> None:
        local_path.unlink(missing_ok=True)

    await _process_with_claude_turn(
        update,
        context,
        session_key=key,
        chat_id=chat_id,
        thread_id=thread_id,
        prompt=prompt,
        label="document",
        drop_message=(f"Queue full ({MAX_QUEUED_MESSAGES}) — file dropped. Wait for current response to finish."),
        queued_message="File queued ({depth}) — will process when current response finishes.",
        on_drop=_cleanup,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def _drain_active_turns(deadline_seconds: int) -> None:
    """Poll until every session finishes its in-flight turn or the deadline hits.

    Used by `/restart` (drain mode) so a user-initiated restart doesn't strand
    a long-running agent turn whose response would otherwise be lost when the
    bridge exits and the subprocess pipe breaks.
    """
    deadline = time.time() + deadline_seconds
    while time.time() < deadline:
        active = _iter_active_sessions()
        if not active:
            logger.info("All active turns drained — proceeding with restart")
            return
        # Filter out sessions whose subprocess already exited; harness wrap-up
        # is in flight and will clear state shortly.
        alive = [(k, s) for k, s in active if s.proc is None or s.proc.poll() is None]
        if not alive:
            logger.info("All Claude subprocesses exited — proceeding with restart")
            return
        await asyncio.sleep(2)
    logger.warning(
        "Drain timeout (%ds) reached with %d turn(s) still active — force-terminating",
        deadline_seconds,
        len(_iter_active_sessions()),
    )


def _session_display_label(session_key: str) -> str:
    """Resolve a human-friendly label for a session key.

    Preference order:
      1. Cached chat/topic title (populated by forum_topic_created/edited).
      2. Project directory, with agent suffix when set (e.g. "Fanta › ernest").
      3. Raw session key as last resort.
    """
    title = get_chat_title(session_key)
    if title:
        return title
    rel_path, agent = _parse_project_entry(_load_chat_projects().get(session_key))
    if rel_path and agent:
        return f"{rel_path} › {agent}"
    if rel_path:
        return rel_path
    if agent:
        return agent
    return session_key


async def handle_forum_topic_event(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cache forum topic names from create/edit service messages.

    Telegram only exposes topic names through these events — regular
    messages in a topic carry the thread id but not the name. We cache
    them under chat_projects.json so /ping (and other future commands)
    can show the topic title instead of a numeric session key.
    """
    msg = update.effective_message
    if msg is None:
        return
    chat_id = update.effective_chat.id if update.effective_chat else None
    thread_id = msg.message_thread_id
    if chat_id is None or thread_id is None:
        return
    name = None
    if msg.forum_topic_created and msg.forum_topic_created.name:
        name = msg.forum_topic_created.name
    elif msg.forum_topic_edited and msg.forum_topic_edited.name:
        name = msg.forum_topic_edited.name
    if not name:
        return
    key = _session_key(chat_id, thread_id)
    try:
        set_chat_title(key, name)
        logger.info("Cached forum topic title for %s: %r", key, name)
    except Exception as e:  # pragma: no cover - storage best-effort
        logger.warning("Failed to cache forum topic title for %s: %s", key, e)


# ---------------------------------------------------------------------------
# Infrastructure: stall detector, lifecycle
# ---------------------------------------------------------------------------


# Set in main() when install_filters runs; consumed by the storm watcher.
_conflict_aggregator = None  # type: ignore[var-annotated]

# Storm watcher tunables — env-overridable for testing.
CONFLICT_STORM_POLL_INTERVAL = max(1, int(os.environ.get("CONFLICT_STORM_POLL_INTERVAL", "30")))
CONFLICT_STORM_THRESHOLD = max(1, int(os.environ.get("CONFLICT_STORM_THRESHOLD", "10")))
CONFLICT_STORM_COOLDOWN = max(1, int(os.environ.get("CONFLICT_STORM_COOLDOWN", "120")))


async def _conflict_storm_watcher() -> None:
    """Watch the conflict aggregator for sustained 409 Conflict storms and
    trigger the stale_telegram_poller self-heal when the rate spikes.

    Closes the loop on part 2: the handler exists; this is what
    actually calls it. A storm means another process is also polling
    Telegram's getUpdates — usually a stale bridge that didn't release
    cleanly. The handler signals SIGTERM via the singleton lockfile and
    polling resumes. After firing, we wait at least CONFLICT_STORM_COOLDOWN
    seconds before considering another storm to give the previous repair
    time to settle.
    """
    from patchbay.self_heal import dispatch_repair

    last_fired = 0.0
    while True:
        await asyncio.sleep(CONFLICT_STORM_POLL_INTERVAL)
        if _conflict_aggregator is None:
            continue
        recent = _conflict_aggregator.recent_count()
        if recent < CONFLICT_STORM_THRESHOLD:
            continue
        now = time.time()
        if now - last_fired < CONFLICT_STORM_COOLDOWN:
            continue
        logger.warning(
            "409 Conflict storm detected (%d in last %ds) — dispatching self-heal",
            recent,
            int(_conflict_aggregator.RECENT_WINDOW_SEC),
        )
        result = dispatch_repair(
            "stale_telegram_poller",
            {"conflict_count": recent},
        )
        last_fired = now
        if result.fixed:
            _conflict_aggregator.reset_recent()


async def _stall_detector() -> None:
    """Background task: kill claude processes that have gone silent.

    Watches `state.last_event_at`, which each harness's `on_progress`
    callback refreshes on every event from the underlying agent. A real
    hang — including a process blocked on a TCC dialog that nobody can
    click — produces zero events; the timestamp stops advancing and we
    kill after STALL_TIMEOUT seconds of silence.
    """
    while True:
        await asyncio.sleep(STALL_POLL_INTERVAL)
        now = time.time()
        # Iterate by harness presence so SDK turns are watched too.
        for key, state in _iter_active_sessions():
            proc = state.proc
            # Subprocess optimization: if the proc already exited, the
            # harness is in its wrap-up phase — clear the timer and move on.
            if proc is not None and proc.poll() is not None:
                state.last_event_at = None
                continue
            if state.last_event_at is None:
                state.last_event_at = now
                continue
            stall_duration = now - state.last_event_at
            if stall_duration >= STALL_TIMEOUT:
                pid = proc.pid if proc is not None else -1
                harness_name = getattr(state.harness, "name", "unknown")
                logger.warning(
                    "Killing stalled Claude turn for %s (harness=%s, pid=%d, idle %.0fs)",
                    key,
                    harness_name,
                    pid,
                    stall_duration,
                )
                await _cancel_session_async(state)
                state.last_event_at = None
                try:
                    mark_stall_kill(key, stall_duration / 60)
                except ValueError:
                    pass  # invalid session key format — skip marker
                _log_activity(
                    "process_kill",
                    session_key=key,
                    pid=pid,
                    reason="stalled",
                    idle_seconds=stall_duration,
                    harness=harness_name,
                )
                if _bot_instance:
                    try:
                        parts = key.split("_", 1)
                        chat_id = int(parts[0])
                        thread_id = int(parts[1]) if len(parts) > 1 else None
                    except (ValueError, IndexError):
                        logger.warning("Cannot parse session key %r for stall notification", key)
                        continue
                    try:
                        send_kwargs: dict = {"chat_id": chat_id}
                        if thread_id is not None:
                            send_kwargs["message_thread_id"] = thread_id
                        await _bot_instance.send_message(
                            text=f"Killed stalled Claude ({stall_duration / 60:.0f} min idle). Send again to retry.",
                            **send_kwargs,
                        )
                    except Exception:
                        logger.debug("Failed to notify about stalled process for %s", key)


async def post_init(app: Application) -> None:
    """Register bot commands and replay any messages lost during previous crash."""
    global _bot_instance
    _bot_instance = app.bot

    from telegram import (
        BotCommand,
        BotCommandScopeAllChatAdministrators,
        BotCommandScopeAllGroupChats,
        BotCommandScopeAllPrivateChats,
        BotCommandScopeChat,
        BotCommandScopeDefault,
    )

    generic_scopes = [
        BotCommandScopeDefault(),
        BotCommandScopeAllPrivateChats(),
        BotCommandScopeAllGroupChats(),
        BotCommandScopeAllChatAdministrators(),
    ]
    for scope in generic_scopes:
        await app.bot.delete_my_commands(scope=scope)
    known_chat_ids = {int(k.split("_")[0]) for k in _load_chat_projects()}
    for chat_id in known_chat_ids:
        try:
            await app.bot.delete_my_commands(scope=BotCommandScopeChat(chat_id=chat_id))
        except Exception as exc:
            logger.debug("delete_my_commands skipped for chat %d: %s", chat_id, exc)

    commands = [
        BotCommand("clearnew", "Start a fresh conversation"),
        BotCommand("setproject", "Set project dir (relative to ~/Developer)"),
        BotCommand("project", "Show current project dir"),
        BotCommand("harness", "Set agent backend (pi)"),
        BotCommand("model", "Set model (opus/sonnet/haiku)"),
        BotCommand("effort", "Set effort level (low/medium/high/xhigh/max)"),
        BotCommand("remote_control", "Start/stop claude remote-control in project dir"),
        BotCommand("cancel", "Soft interrupt (SIGINT) — try before /kill"),
        BotCommand("kill", "Hard kill (SIGKILL) active Claude process"),
        BotCommand("restart", "Restart the bridge"),
        BotCommand("ping", "Check if bridge is alive"),
        BotCommand("usage", "Show Claude Code quota (tokens + block time)"),
        BotCommand("activity", "Recent activity.jsonl entries (optional event filter)"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info("Bot commands registered with Telegram")

    asyncio.create_task(_stall_detector())
    logger.info(
        "Stall detector started (poll=%ds, timeout=%ds)",
        STALL_POLL_INTERVAL,
        STALL_TIMEOUT,
    )
    asyncio.create_task(_conflict_storm_watcher())
    logger.info(
        "Conflict storm watcher started (poll=%ds, threshold=%d/min, cooldown=%ds)",
        CONFLICT_STORM_POLL_INTERVAL,
        CONFLICT_STORM_THRESHOLD,
        CONFLICT_STORM_COOLDOWN,
    )

    # Clean up stale photo files from prior crash/SIGKILL (older than 1 hour)
    try:
        now = time.time()
        for photo_file in PHOTO_DIR.glob("*.jpg"):
            try:
                age = now - photo_file.stat().st_mtime
                if age > 3600:
                    photo_file.unlink(missing_ok=True)
                    logger.info("Cleaned up stale photo: %s (age %.0fs)", photo_file.name, age)
            except OSError:
                pass
    except Exception:
        logger.debug("Failed to clean up stale photos")

    # Clean up expired session files
    try:
        import time as _time

        now = _time.time()
        for sf in SESSION_DIR.glob("*.json"):
            try:
                data = json.loads(sf.read_text())
                if now - data.get("last_active", 0) > SESSION_EXPIRY:
                    sf.unlink()
                    logger.info("Cleaned up expired session file: %s", sf.name)
            except (json.JSONDecodeError, KeyError):
                sf.unlink()
    except Exception:
        logger.debug("Failed to clean up expired sessions")

    asyncio.create_task(replay_pending(app.bot))

    restart_notify = RESTART_NOTIFY_FILE
    if restart_notify.exists():
        try:
            data = json.loads(restart_notify.read_text())
            send_kwargs: dict = {
                "chat_id": data["chat_id"],
                "text": "Bridge restarted ✓",
            }
            if data.get("thread_id"):
                send_kwargs["message_thread_id"] = data["thread_id"]
            await app.bot.send_message(**send_kwargs)
        except Exception as exc:
            logger.warning("Could not send restart confirmation: %s: %s", type(exc).__name__, exc)
        restart_notify.unlink(missing_ok=True)


def _graceful_shutdown(signum: int, frame) -> None:
    """Handle SIGTERM/SIGINT: stop accepting new messages, wait for active
    processes, clean up temp files, then exit."""
    global _shutting_down
    sig_name = signal.Signals(signum).name
    logger.info("Received %s — starting graceful shutdown", sig_name)
    _shutting_down = True

    for key, proc in _iter_active_procs():
        if proc.poll() is None:
            logger.info("Sending SIGTERM to Claude process for %s (pid %d)", key, proc.pid)
            proc.terminate()

    deadline = time.time() + SHUTDOWN_PROCESS_TIMEOUT
    for key, proc in _iter_active_procs():
        remaining = max(0, deadline - time.time())
        try:
            proc.wait(timeout=remaining)
            logger.info("Claude process for %s exited cleanly", key)
        except subprocess.TimeoutExpired:
            logger.warning("Force-killing Claude process for %s (pid %d)", key, proc.pid)
            proc.kill()
            proc.wait()

    if _remote_proc and _remote_proc.poll() is None:
        logger.info("Terminating remote-control process (pid %d)", _remote_proc.pid)
        _remote_proc.terminate()
        try:
            _remote_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _remote_proc.kill()

    try:
        for f in PHOTO_DIR.glob("*.jpg"):
            f.unlink(missing_ok=True)
        logger.info("Cleaned up temp photo directory")
    except Exception as exc:
        logger.warning("Photo dir cleanup failed during shutdown: %s: %s", type(exc).__name__, exc)

    logger.info("Graceful shutdown complete — exiting")
    sys.exit(0)


# ---------------------------------------------------------------------------
# Telegram send helpers — re-exported from patchbay.telegram_send so existing
# call sites and test patches against `bridge.X` keep working after the split.
# The implementations reach back through `bridge` for the config constants and
# helper functions they consume (SEND_RETRY_*, TYPING_INTERVAL, _to_markdownv2,
# _log_activity, telegramify_markdown, etc.), so monkeypatching `bridge.X`
# remains effective and log records still surface on the `bridge` logger.
# ---------------------------------------------------------------------------
from patchbay.telegram_send import (  # noqa: E402, F401
    TYPING_MAX_FAILURES,
    _MARKDOWN_FAILURE_TEXT_LIMIT,
    _is_noisy_status,
    _is_silence_narration,
    _notify_delivery_failure,
    _run_heartbeat,
    _send_response,
    _to_markdownv2,
    keep_typing,
)


# ---------------------------------------------------------------------------
# Command handlers — re-exported from patchbay.commands so external callers
# (tests doing `bridge.cmd_X`, registrations in main()) keep working after
# the split. The actual definitions live in patchbay/commands/<group>.py;
# they reference bridge module attributes at call time, so this re-export
# only needs to happen after every bridge module-global they touch is
# defined — which is true here, just before main().
# ---------------------------------------------------------------------------
from patchbay.commands.lifecycle import (  # noqa: E402, F401
    cmd_cancel,
    cmd_clearnew,
    cmd_kill,
    cmd_ping,
    cmd_restart,
    cmd_start,
)
from patchbay.commands.context import (  # noqa: E402, F401
    _SUMMARIZE_PROMPT,
    _build_handoff_prompt,
    cmd_compact,
    cmd_context,
)
from patchbay.commands.observability import (  # noqa: E402, F401
    _bar,
    _block_time_percent,
    _ccusage_report,
    _format_bytes,
    _format_tokens,
    _format_uptime,
    _safe_count,
    _week_start_yyyymmdd,
    _week_time_percent,
    cmd_activity,
    cmd_health,
    cmd_soak,
    cmd_usage,
)
from patchbay.commands.project import (  # noqa: E402, F401
    callback_effort,
    callback_model,
    callback_setproject,
    cmd_effort,
    cmd_harness,
    cmd_model,
    cmd_project,
    cmd_remote_control,
    cmd_setproject,
)
from patchbay.commands.heartbeat import (  # noqa: E402, F401
    callback_heartbeat,
    cmd_heartbeat,
)


def main() -> None:
    # Single-instance guard FIRST — before any Telegram polling starts.
    # Prevents two bridges racing on getUpdates (409 storm).
    from patchbay.config import BASE_DIR
    from patchbay.log_filters import install_filters
    from patchbay.logrotate import rotate_startup_logs
    from patchbay.singleton import acquire_singleton

    acquire_singleton()
    global _conflict_aggregator
    _conflict_aggregator = install_filters()

    # Rotate oversize bridge.err / bridge.log and re-point sys.stdout/stderr
    # at fresh files. Launchd's stderr redirect happens at exec time, so the
    # only moment we can reclaim a fresh fd is here, at Python startup.
    rotate_startup_logs(BASE_DIR / "logs")

    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    app = Application.builder().token(BOT_TOKEN.reveal()).concurrent_updates(True).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clearnew", cmd_clearnew))
    app.add_handler(CommandHandler("setproject", cmd_setproject))
    app.add_handler(CallbackQueryHandler(callback_setproject, pattern=r"^setproject:"))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(callback_model, pattern=r"^model:"))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("harness", cmd_harness))
    app.add_handler(CommandHandler("heartbeat", cmd_heartbeat))
    app.add_handler(CallbackQueryHandler(callback_heartbeat, pattern=r"^heartbeat:"))
    app.add_handler(CallbackQueryHandler(callback_effort, pattern=r"^effort:"))
    app.add_handler(CommandHandler("remote_control", cmd_remote_control))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("activity", cmd_activity))
    app.add_handler(CommandHandler("soak", cmd_soak))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(
        MessageHandler(
            filters.StatusUpdate.FORUM_TOPIC_CREATED | filters.StatusUpdate.FORUM_TOPIC_EDITED,
            handle_forum_topic_event,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    logger.info(
        "Bridge started (max_workers=%d). Polling for Telegram messages...",
        MAX_WORKERS,
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
