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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import telegramify_markdown  # noqa: E402
from telegramify_markdown.customize import get_runtime_config  # noqa: E402
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

# Configure telegramify-markdown: no emoji prefixes for headings
_tgmd_config = get_runtime_config()
_tgmd_sym = _tgmd_config.markdown_symbol
_tgmd_sym.head_level_1 = ">"
_tgmd_sym.head_level_2 = ">>"
_tgmd_sym.head_level_3 = ">>>"
_tgmd_sym.head_level_4 = ">>>"
_tgmd_sym.image = ""
_tgmd_sym.link = ""

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
    CLAUDE_PATH,
    DEFAULT_HARNESS,
    FORGE_QUEUE_DIR,  # noqa: F401 — used by tests via bridge.FORGE_QUEUE_DIR
    MAX_QUEUED_MESSAGES,
    MAX_TIMEOUT,
    MAX_TURNS,
    MAX_WORKERS,
    PA_PLUGIN_DIR,
    PENDING_DIR,
    DOC_DIR,
    PHOTO_DIR,
    QUOTA_HIT_PREFIX,
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
    ClaudeCliHarness,
    ClaudeSdkHarness,
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
    split_paired_for_telegram,
)
from patchbay.models import (  # noqa: E402
    VALID_MODELS,
    extract_model_prefix,
    get_chat_model,
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

    proc: subprocess.Popen | None = None  # cc-cli only; mirrored by harness's proc_setter
    harness: object | None = None  # whichever harness instance is driving the active turn
    worker_loop: asyncio.AbstractEventLoop | None = None  # loop owned by _drive_harness_sync; needed for cross-loop cancel of cc-sdk
    started_at: float | None = None  # time.time() when processing began
    last_event_at: float | None = None  # last time stall-detector saw activity
    queue: list[QueuedMessage] = field(default_factory=list)  # debounced messages awaiting processing
    processing: bool = False  # True while a claude run is in flight for this key
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_sessions: dict[str, SessionState] = {}


def _get_session_state(key: str) -> SessionState:
    """Return the SessionState for `key`, creating an empty one if needed."""
    state = _sessions.get(key)
    if state is None:
        state = SessionState()
        _sessions[key] = state
    return state


def _iter_active_procs() -> list[tuple[str, subprocess.Popen]]:
    """Snapshot of (session_key, proc) for every session with a live subprocess.

    cc-cli only — sessions where the harness owns its own subprocess
    (cc-sdk) won't appear here. Use `_iter_active_sessions` for the
    backend-agnostic view (e.g. /ping, stall detector).
    """
    return [(k, s.proc) for k, s in _sessions.items() if s.proc is not None]


def _iter_active_sessions() -> list[tuple[str, "SessionState"]]:
    """Snapshot of (session_key, state) for every session running a turn,
    regardless of harness. Used by /ping, the stall detector, /restart,
    and graceful shutdown so cc-sdk turns are visible too.

    A session counts as active when either `state.proc` is set (cc-cli)
    or `state.harness` is set (cc-sdk, or cc-cli before the proc is
    spawned and after it is reaped). Either signal alone is sufficient.
    """
    return [
        (k, s)
        for k, s in _sessions.items()
        if s.proc is not None or s.harness is not None
    ]


async def _cancel_session_async(state: "SessionState") -> None:
    """Best-effort cancel of the in-flight turn for a session.

    Backend-agnostic dispatch:
      * cc-cli — `state.proc` is set; SIGKILL via `proc.kill()`. Sync,
        immediate; the harness's drain returns and `_drive_harness_sync`
        exits naturally.
      * cc-sdk — no subprocess handle the bridge can reach (the SDK
        owns it). Schedule `harness.cancel()` on the worker-thread loop
        captured in `state.worker_loop` via `run_coroutine_threadsafe`,
        await with a short timeout so a wedged loop can't hang us.

    Idempotent and silent on error — callers (cmd_kill, stall detector,
    shutdown) treat this as a fire-and-forget request.
    """
    proc = state.proc
    if proc is not None:
        try:
            proc.kill()
        except OSError:
            pass
        return

    harness = state.harness
    loop = state.worker_loop
    if harness is None or loop is None:
        return
    try:
        future = asyncio.run_coroutine_threadsafe(harness.cancel(), loop)
    except RuntimeError:
        # Worker loop closed between our read and the schedule — nothing to do.
        return
    try:
        await asyncio.wait_for(asyncio.wrap_future(future), timeout=5.0)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — silence everything during cancel
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

# Captured once at import time — used by /health to report process uptime.
_BRIDGE_STARTED_AT = time.time()


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
    `_cancel_session_async` reads those fields to dispatch a cancel:
    cc-cli is killed via `state.proc.kill()` (same as before), cc-sdk
    is cancelled via `run_coroutine_threadsafe(harness.cancel(),
    state.worker_loop)`. The fields are cleared in `finally`.
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
    """Invoke claude via ClaudeCliHarness, translate the event stream to a string.

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
        model = get_chat_model(session_key)
    effort = resolve_effort(session_key)

    # Resolve harness: per-chat override > DEFAULT_HARNESS env. Both
    # cc-cli and cc-sdk are dispatched as of phase 3b — selection is
    # logged on every activity entry under `harness=` and
    # `harness_requested=` so the live-soak comparison can grep
    # behavioural diffs.
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
        "claude_invoke",
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

    if effective_harness == "cc-sdk":
        # cc-sdk owns its subprocess internally — there's no Popen handle
        # for the bridge to mirror, so /kill / stall detector / shutdown
        # route through `harness.cancel()` via _cancel_session_async
        # instead. `state.proc` stays None for the duration of this turn.
        harness = ClaudeSdkHarness(
            cli_path=CLAUDE_PATH,
            max_timeout_seconds=MAX_TIMEOUT,
            on_progress=_on_progress,
            max_turns_default=(
                max_turns_override if max_turns_override is not None else MAX_TURNS
            ),
        )
    elif effective_harness == "pi":
        # Pi (badlogicgames/pi) — multi-model coding agent. Uses its own
        # session storage (~/.pi/agent/sessions). Subprocess like cc-cli
        # so proc_setter mirrors into state.proc for /kill / stall.
        from patchbay.harness import PiHarness

        harness = PiHarness(
            max_timeout_seconds=MAX_TIMEOUT,
            on_progress=_on_progress,
            proc_setter=_proc_setter,
        )
    elif effective_harness == "aider":
        # Aider — model-agnostic coding CLI. session_id is a chat-history
        # file path (we own ./aider-history/<session-key>.md). Default
        # model openrouter/deepseek/deepseek-chat (override via
        # PATCHBAY_AIDER_MODEL env (or legacy STARGATE_AIDER_MODEL) or per-chat /model).
        from patchbay.harness import AiderHarness

        harness = AiderHarness(
            max_timeout_seconds=MAX_TIMEOUT,
            on_progress=_on_progress,
            proc_setter=_proc_setter,
        )
    elif effective_harness == "opencode":
        # sst/opencode — JSON event protocol via `opencode run --format json`.
        # Default model `openrouter/deepseek/deepseek-chat-v3.1` (override via
        # PATCHBAY_OPENCODE_MODEL env (or legacy STARGATE_OPENCODE_MODEL) or per-chat /model).
        from patchbay.harness import OpenCodeHarness

        harness = OpenCodeHarness(
            max_timeout_seconds=MAX_TIMEOUT,
            on_progress=_on_progress,
            proc_setter=_proc_setter,
        )
    else:
        harness = ClaudeCliHarness(
            claude_path=CLAUDE_PATH,
            max_timeout_seconds=MAX_TIMEOUT,
            on_progress=_on_progress,
            proc_setter=_proc_setter,
            max_turns_default=(
                max_turns_override if max_turns_override is not None else MAX_TURNS
            ),
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
                "claude_timeout",
                session_key=session_key,
                duration=duration,
                elapsed_ms=int(duration * 1000),
                harness=effective_harness,
            )
            return (
                f"[Timed out after {MAX_TIMEOUT // 60} min] "
                "Session preserved — send your message again to resume."
            )

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
                "claude_complete",
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
            "claude_error",
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
            "claude_complete",
            session_key=session_key,
            duration=duration,
            elapsed_ms=int(duration * 1000),
            turns_used=final.num_turns,
            exit_code=0,
            response_len=len(final.raw_text),
            harness=effective_harness,
        )
        response = final.raw_text or "(no parseable response)"

        # Empty-success: claude finished cleanly but produced no final text.
        # One-shot summary retry against the freshly-saved session_id; gives
        # the user a real reply instead of the "(Completed N turns…)" placeholder.
        if is_empty_success_response(response) and not _retry:
            new_session_id = get_session_id(session_key)
            if new_session_id:
                summary = _request_summary(session_key, new_session_id, chat_cwd)
                if summary:
                    _log_activity(
                        "summary_retry_success",
                        session_key=session_key,
                        response_len=len(summary),
                    )
                    return summary
                _log_activity("summary_retry_empty", session_key=session_key)
            else:
                _log_activity("summary_retry_skipped_no_session", session_key=session_key)

        return response

    # Defensive: harness didn't terminate properly. Treat as no output.
    _log_activity(
        "claude_error",
        session_key=session_key,
        duration=duration,
        elapsed_ms=int(duration * 1000),
        error="harness returned no terminator",
        harness=effective_harness,
    )
    return "(no output)"


def _request_summary(session_key: str, session_id: str, chat_cwd: str) -> str | None:
    """Re-invoke claude --resume <id> with a short summarize prompt.

    Used when the primary invocation finished successfully but produced no
    final text. Returns the summary string on success, or None if the
    summary attempt also yielded no usable text. Bounded by --max-turns 5
    and a 120s wall-clock timeout — this should be one quick text turn."""
    summary_cmd = [
        CLAUDE_PATH,
        "-p",
        "Summarize what you just did in 1-3 sentences. End with a plain text reply.",
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--max-turns",
        "5",
        "--resume",
        session_id,
    ]
    logger.info("Empty-success retry for %s — requesting summary", session_key)
    try:
        proc = subprocess.run(
            summary_cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=chat_cwd,
        )
    except subprocess.TimeoutExpired:
        logger.warning("Summary retry for %s timed out", session_key)
        return None
    if proc.returncode != 0:
        logger.warning("Summary retry for %s exited %d", session_key, proc.returncode)
        return None
    summary = parse_claude_response(proc.stdout, session_key)
    if is_empty_success_response(summary) or summary.startswith("(no "):
        return None
    return summary


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


TYPING_MAX_FAILURES = 5


async def keep_typing(
    chat_id: int,
    thread_id: int | None,
    stop_event: asyncio.Event,
    bot,
) -> None:
    """Send typing indicator every few seconds until stop_event is set.

    Error policy (nothing is silently swallowed):

      * ``Forbidden`` / ``ChatMigrated`` — persistent; log once and give up
        immediately. The bot has been blocked, kicked, or the chat moved.
      * ``RetryAfter`` — honor the server-requested backoff instead of the
        default interval.
      * Other exceptions — log at warning, keep trying; give up after
        ``TYPING_MAX_FAILURES`` consecutive failures. Success resets the
        counter so a periodic transient blip never trips the cap.
      * ``CancelledError`` — propagate so awaiters see the cancellation.
    """
    kwargs = {"chat_id": chat_id, "action": "typing"}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id

    failures = 0
    wait_seconds: float = TYPING_INTERVAL
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(**kwargs)
            failures = 0
            wait_seconds = TYPING_INTERVAL
        except asyncio.CancelledError:
            raise
        except (Forbidden, ChatMigrated) as exc:
            logger.warning(
                "keep_typing giving up for chat=%s thread=%s: %s (persistent)",
                chat_id,
                thread_id,
                type(exc).__name__,
            )
            return
        except RetryAfter as exc:
            wait_seconds = float(getattr(exc, "retry_after", TYPING_INTERVAL))
            logger.warning(
                "keep_typing rate-limited for chat=%s thread=%s; backing off %.1fs",
                chat_id,
                thread_id,
                wait_seconds,
            )
            # do not count RetryAfter toward the give-up cap — Telegram told
            # us to wait, not that we've failed.
        except Exception as exc:
            failures += 1
            log_fn = logger.error if failures >= TYPING_MAX_FAILURES else logger.warning
            log_fn(
                "keep_typing failed for chat=%s thread=%s (%d/%d): %s: %s",
                chat_id,
                thread_id,
                failures,
                TYPING_MAX_FAILURES,
                type(exc).__name__,
                exc,
            )
            if failures >= TYPING_MAX_FAILURES:
                logger.error(
                    "keep_typing giving up for chat=%s thread=%s after %d consecutive failures",
                    chat_id,
                    thread_id,
                    failures,
                )
                return

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=wait_seconds)
            return
        except asyncio.TimeoutError:
            continue


_MARKDOWN_FAILURE_TEXT_LIMIT = 800


def _to_markdownv2(text: str) -> str | None:
    """Convert markdown to Telegram MarkdownV2. Returns None on failure.

    On conversion failure, log the raw text (truncated) and the exception
    to activity.jsonl as event=markdown_conversion_failed so we can come
    back later and reproduce the bug in the converter. Without this, a
    quiet plain-text fallback hides converter regressions.
    """
    try:
        return telegramify_markdown.markdownify(text)
    except Exception as exc:
        _log_activity(
            "markdown_conversion_failed",
            error_type=type(exc).__name__,
            error=str(exc)[:300],
            raw_text=text[:_MARKDOWN_FAILURE_TEXT_LIMIT],
            raw_text_len=len(text),
            truncated=len(text) > _MARKDOWN_FAILURE_TEXT_LIMIT,
        )
        logger.warning(
            "MarkdownV2 conversion failed (%s): %s — see activity.jsonl for raw text",
            type(exc).__name__,
            str(exc)[:120],
        )
        return None


async def _send_response(bot, chat_id: int, thread_id: int | None, response: str) -> None:
    """Send a response, splitting at Telegram's message limit.

    The response is converted to MarkdownV2 *once as a whole* (not per
    chunk) and then split on paragraph > line > word boundaries. Splitting
    converted text — rather than slicing the raw response and converting
    each slice independently — prevents mid-`*bold*` cuts that yielded
    Telegram's `BadRequest: can't find end of bold entity` errors. After
    splitting, each chunk's toggle entities (`*`, `_`, ``` `, ``` ``` ```,
    `~`, `||`) are checked for parity; if any chunk is unbalanced, the
    entire response downgrades to plain text (the same all-or-nothing
    invariant the previous chunker enforced — no half-formatted output).

    If a chunk's MarkdownV2 send fails mid-response, remaining chunks
    downgrade to plain too. Retries each chunk up to SEND_RETRY_ATTEMPTS
    times with exponential backoff.

    Every send attempt's outcome is recorded to the outbound audit log
    (source="claude-response") for diagnosing client-side render drops.
    Audit failures are swallowed: they must never affect user-visible
    send behavior.
    """
    send_kwargs: dict = {"chat_id": chat_id}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id
    audit_session_key = _session_key(chat_id, thread_id)

    response, file_requests = extract_file_sentinels(response)

    if not response and not file_requests:
        return

    # Convert the whole response once; pair raw + md slices via paragraph
    # alignment so each chunk's audit log carries the matching source slice
    # and any mid-response downgrade has a sensible plain fallback for the
    # remaining chunks.
    converted_full = _to_markdownv2(response) if response else None
    raw_chunks: list[str]
    md_chunks: list[str | None]
    if converted_full is None:
        raw_chunks = split_for_telegram(response, TELEGRAM_MSG_LIMIT) if response else []
        md_chunks = [None] * len(raw_chunks)
        use_markdown = False
    else:
        pairs = split_paired_for_telegram(response, converted_full, TELEGRAM_MSG_LIMIT)
        md_pieces = [m for _, m in pairs]
        if all(is_markdownv2_balanced(m) for m in md_pieces):
            raw_chunks = [r for r, _ in pairs]
            md_chunks = list(md_pieces)
            use_markdown = True
        else:
            _log_activity(
                "markdown_chunk_unbalanced",
                chunk_total=len(md_pieces),
                converted_len=len(converted_full),
                response_len=len(response),
            )
            logger.warning(
                "MarkdownV2 chunk parity check failed (%d chunks); "
                "downgrading entire response to plain to avoid Telegram "
                "entity rejection",
                len(md_pieces),
            )
            raw_chunks = split_for_telegram(response, TELEGRAM_MSG_LIMIT)
            md_chunks = [None] * len(raw_chunks)
            use_markdown = False

    chunk_total = len(raw_chunks)
    if not chunk_total and not file_requests:
        return

    for chunk_index, chunk in enumerate(raw_chunks):
        md_chunk = md_chunks[chunk_index] if use_markdown else None
        last_exc: Exception | None = None
        for attempt in range(SEND_RETRY_ATTEMPTS):
            sent_as_md = md_chunk is not None
            try:
                if sent_as_md:
                    await bot.send_message(
                        text=md_chunk,
                        parse_mode=ParseMode.MARKDOWN_V2,
                        **send_kwargs,
                    )
                else:
                    await bot.send_message(text=chunk, **send_kwargs)
                last_exc = None
                try:
                    log_outbound_response(
                        session_key=audit_session_key,
                        chunk_index=chunk_index,
                        chunk_total=chunk_total,
                        raw=chunk,
                        md=md_chunk if sent_as_md else None,
                        parse_mode="MarkdownV2" if sent_as_md else "plain",
                        status="ok",
                    )
                except Exception as audit_exc:
                    logger.debug("outbound audit log failed (success path): %s", audit_exc)
                break
            except Exception as e:
                last_exc = e
                # MarkdownV2 send failed: drop to plain for this attempt and
                # propagate the downgrade to all remaining chunks of this
                # response so we don't ship a half-formatted message.
                if md_chunk is not None:
                    # Log raw + md so we can reproduce the converter bug or
                    # the Telegram-rejected payload later.
                    _log_activity(
                        "markdown_send_failed",
                        error_type=type(e).__name__,
                        error=str(e)[:300],
                        chunk_index=chunk_index,
                        chunk_total=chunk_total,
                        raw_text=chunk[:_MARKDOWN_FAILURE_TEXT_LIMIT],
                        md_text=md_chunk[:_MARKDOWN_FAILURE_TEXT_LIMIT],
                        raw_text_len=len(chunk),
                        md_text_len=len(md_chunk),
                    )
                    logger.warning(
                        "MarkdownV2 send rejected (%s) chunk %d/%d, "
                        "switching this and all remaining chunks to plain — "
                        "see activity.jsonl markdown_send_failed for raw+md",
                        type(e).__name__,
                        chunk_index + 1,
                        chunk_total,
                    )
                    md_chunk = None
                    use_markdown = False
                delay = SEND_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "Telegram send failed (attempt %d/%d) for chat=%s thread=%s chunk %d/%d: %s — retrying in %.1fs",
                    attempt + 1,
                    SEND_RETRY_ATTEMPTS,
                    chat_id,
                    thread_id,
                    chunk_index + 1,
                    chunk_total,
                    e,
                    delay,
                )
                if attempt < SEND_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(delay)
        if last_exc is not None:
            logger.error(
                "Telegram send failed after %d attempts for chat=%s thread=%s chunk %d/%d: %s",
                SEND_RETRY_ATTEMPTS,
                chat_id,
                thread_id,
                chunk_index + 1,
                chunk_total,
                last_exc,
            )
            try:
                log_outbound_response(
                    session_key=audit_session_key,
                    chunk_index=chunk_index,
                    chunk_total=chunk_total,
                    raw=chunk,
                    md=None,
                    parse_mode="plain" if md_chunk is None else "MarkdownV2",
                    status=type(last_exc).__name__,
                )
            except Exception as audit_exc:
                logger.debug("outbound audit log failed (error path): %s", audit_exc)
            raise last_exc

    if file_requests:
        await send_files(
            bot,
            chat_id=chat_id,
            thread_id=thread_id,
            session_key=audit_session_key,
            requests=file_requests,
        )


async def _notify_delivery_failure(bot, chat_id: int, thread_id: int | None, label: str) -> None:
    """Attempt to notify the user that a response failed to deliver."""
    send_kwargs: dict = {"chat_id": chat_id}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id
    try:
        await bot.send_message(
            text="[Response was generated but could not be delivered due to a Telegram error. Check logs for details.]",
            **send_kwargs,
        )
    except Exception as notify_exc:
        logger.error(
            "Also failed to send delivery-failure notification for %s: %s",
            label,
            notify_exc,
        )


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


def _maybe_handoff_quota(
    response: str, session_key: str, chat_id: int, thread_id: int | None
) -> str:
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
    return (
        "Hit a quota/rate limit. Tried to hand off to Forge but "
        "failed to write the queue file. Try again later."
    )


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

    try:
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                _executor, lambda: run_claude(prompt, session_key, model=model)
            )
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
                else "\n\n---\n\n".join(
                    f"[Follow-up {i + 1}]\n{item.text}" for i, item in enumerate(batch)
                )
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
    await _process_with_claude_turn(
        update,
        context,
        session_key=key,
        chat_id=chat_id,
        thread_id=thread_id,
        prompt=clean_text,
        label="message",
        drop_message=(
            f"Queue full ({MAX_QUEUED_MESSAGES}) — message dropped. "
            "Wait for current response to finish."
        ),
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
        drop_message=(
            f"Queue full ({MAX_QUEUED_MESSAGES}) — photo dropped. "
            "Wait for current response to finish."
        ),
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
        drop_message=(
            f"Queue full ({MAX_QUEUED_MESSAGES}) — file dropped. "
            "Wait for current response to finish."
        ),
        queued_message="File queued ({depth}) — will process when current response finishes.",
        on_drop=_cleanup,
    )


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Patchbay active.\nYour Telegram user ID: {uid}\n\n"
        "Commands:\n"
        "/clearnew - Start a fresh conversation (in current topic)\n"
        "/setproject <path> - Set project dir (relative to ~/Developer)\n"
        "/setproject - Clear project binding (use default)\n"
        "/project - Show current project dir\n"
        "/model - Set model (opus/sonnet/haiku) or prefix with !s !o !h\n"
        "/effort - Set effort level (low/medium/high/xhigh/max)\n"
        "/harness - Show or set the agent backend (cc-cli/cc-sdk)\n"
        "/remote-control - Start claude remote-control in this topic's project dir\n"
        "/remote-control stop - Stop remote-control\n"
        "/kill - Kill active Claude process\n"
        "/restart - Restart the bridge\n"
        "/ping - Check if bridge is alive\n"
        "/health - Disk, queues, uptime, counts\n"
        "/activity [event] [count] - Recent activity.jsonl entries (e.g. /activity self_heal)\n"
        "/soak [since] [session] - Compare harness backends (e.g. /soak 7d)\n"
        "/context - Show context-window usage for this chat (cc-sdk only)\n"
        "/compact [steering] - Compact the running context (cc-sdk only)\n"
        "/usage - Show Claude Code quota (tokens + block time remaining)\n\n"
        "Each forum topic runs as an independent Claude session."
    )


async def cmd_clearnew(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)
    clear_session(key)
    await update.message.reply_text("Fresh session started.")
    logger.info("Session cleared for %s", key)


async def cmd_setproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = context.args
    if not args:
        projects = _get_all_projects()
        buttons = [[InlineKeyboardButton(name, callback_data=f"setproject:{name}")] for name in projects]
        buttons.append([InlineKeyboardButton("Clear (use ~/Developer)", callback_data="setproject:__clear__")])
        await update.message.reply_text(
            "Pick a project (A-Z).\nOr type: /setproject <path>",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    rel_path = args[0]
    # Path traversal prevention
    if ".." in rel_path or rel_path.startswith("/") or "\\" in rel_path:
        await update.message.reply_text("Invalid project path.")
        return
    abs_path = os.path.join(WORKING_DIR, rel_path)
    real_path = os.path.realpath(abs_path)
    if not real_path.startswith(os.path.realpath(WORKING_DIR)):
        await update.message.reply_text("Invalid project path.")
        return
    if not os.path.isdir(abs_path):
        await update.message.reply_text(f"Directory not found: ~/Developer/{rel_path}")
        return

    set_chat_project(key, rel_path)
    clear_session(key)
    chat_title = update.effective_chat.title or "DM"
    await update.message.reply_text(
        f"Project set: ~/Developer/{rel_path}\nChat: {chat_title}\nSession reset. Claude will run from this directory."
    )
    logger.info("Project set to %s for %s (%s)", rel_path, key, chat_title)


async def callback_setproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for project selection."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    thread_id = query.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    data = query.data  # "setproject:<name>" or "setproject:__clear__"
    rel_path = data.split(":", 1)[1]

    if rel_path == "__clear__":
        set_chat_project(key, None)
        clear_session(key)
        await query.edit_message_text("Project cleared. Using default: ~/Developer\nSession reset.")
        logger.info("Project cleared for %s", key)
        return

    # Path traversal prevention: reject suspicious paths
    if ".." in rel_path or rel_path.startswith("/") or "\\" in rel_path:
        await query.edit_message_text("Invalid project path.")
        return

    abs_path = os.path.join(WORKING_DIR, rel_path)
    # Verify resolved path is still under WORKING_DIR
    real_path = os.path.realpath(abs_path)
    if not real_path.startswith(os.path.realpath(WORKING_DIR)):
        await query.edit_message_text("Invalid project path.")
        return
    if not os.path.isdir(abs_path):
        await query.edit_message_text(f"Directory not found: ~/Developer/{rel_path}")
        return

    set_chat_project(key, rel_path)
    clear_session(key)
    chat_title = update.effective_chat.title or "DM"
    await query.edit_message_text(
        f"Project set: ~/Developer/{rel_path}\nChat: {chat_title}\nSession reset. Claude will run from this directory."
    )
    logger.info("Project set to %s for %s (%s)", rel_path, key, chat_title)


async def cmd_project(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)
    agent = get_chat_agent(key)
    rel_path_entry, _ = _parse_project_entry(_load_chat_projects().get(key))
    if rel_path_entry:
        label = f"Project: ~/Developer/{rel_path_entry}"
        if agent:
            label += f" (agent: {agent})"
        await update.message.reply_text(label)
    else:
        await update.message.reply_text("No project set. Using default: ~/Developer")


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the model for this chat/topic."""
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = context.args
    if args:
        choice = args[0].lower()
        if choice == "default":
            set_chat_model(key, None)
            await update.message.reply_text("Model reset to default (opus).")
            logger.info("Model cleared for %s", key)
            return
        if choice not in VALID_MODELS:
            await update.message.reply_text("Invalid model. Choose: opus, sonnet, haiku, default")
            return
        set_chat_model(key, choice)
        await update.message.reply_text(f"Model set to {choice}. Takes effect on next message.")
        logger.info("Model set to %s for %s", choice, key)
        return

    # No args: show buttons
    current = get_chat_model(key) or "default (opus)"
    buttons = [
        [
            InlineKeyboardButton("opus", callback_data="model:opus"),
            InlineKeyboardButton("sonnet", callback_data="model:sonnet"),
            InlineKeyboardButton("haiku", callback_data="model:haiku"),
        ],
        [InlineKeyboardButton("default", callback_data="model:__default__")],
    ]
    await update.message.reply_text(
        f"Current model: {current}\nPick a model (or prefix any message with !s !o !h for one-shot):",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_model(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for model selection."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    thread_id = query.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    choice = query.data.split(":", 1)[1]
    if choice == "__default__":
        set_chat_model(key, None)
        await query.edit_message_text("Model reset to default (opus).")
        logger.info("Model cleared for %s", key)
        return
    if choice not in VALID_MODELS:
        await query.edit_message_text(f"Invalid model: {choice}")
        return

    set_chat_model(key, choice)
    await query.edit_message_text(f"Model set to {choice}. Takes effect on next message.")
    logger.info("Model set to %s for %s", choice, key)


async def cmd_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the effort level for this chat/topic."""
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = context.args
    if args:
        choice = args[0].lower()
        if choice == "default":
            set_chat_effort(key, None)
            await update.message.reply_text(f"Effort reset to default ({DEFAULT_EFFORT}).")
            logger.info("Effort cleared for %s", key)
            return
        if choice not in VALID_EFFORTS:
            await update.message.reply_text(f"Invalid effort. Choose: {', '.join(VALID_EFFORTS)}, default")
            return
        set_chat_effort(key, choice)
        await update.message.reply_text(f"Effort set to {choice}. Takes effect on next message.")
        logger.info("Effort set to %s for %s", choice, key)
        return

    current = get_chat_effort(key) or f"default ({DEFAULT_EFFORT})"
    buttons = [
        [InlineKeyboardButton(level, callback_data=f"effort:{level}") for level in VALID_EFFORTS],
        [InlineKeyboardButton("default", callback_data="effort:__default__")],
    ]
    await update.message.reply_text(
        f"Current effort: {current}\nPick an effort level:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


async def callback_effort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline keyboard button presses for effort selection."""
    query = update.callback_query
    await query.answer()

    chat_id = update.effective_chat.id
    thread_id = query.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    choice = query.data.split(":", 1)[1]
    if choice == "__default__":
        set_chat_effort(key, None)
        await query.edit_message_text(f"Effort reset to default ({DEFAULT_EFFORT}).")
        logger.info("Effort cleared for %s", key)
        return
    if choice not in VALID_EFFORTS:
        await query.edit_message_text(f"Invalid effort: {choice}")
        return

    set_chat_effort(key, choice)
    await query.edit_message_text(f"Effort set to {choice}. Takes effect on next message.")
    logger.info("Effort set to %s for %s", choice, key)


async def cmd_harness(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set or show the agent backend harness for this chat/topic.

    `/harness` shows the current selection (per-chat override or the
    DEFAULT_HARNESS fallback). `/harness <name>` sets it for this topic;
    `/harness default` clears the override.

    Both cc-cli and cc-sdk are dispatched live (phase 3b). Switch
    freely; activity.jsonl tags every turn with `harness=<effective>`
    for the live-soak comparison.
    """
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = context.args
    if args:
        choice = args[0].lower()
        if choice == "default":
            set_chat_harness(key, None)
            await update.message.reply_text(
                f"Harness reset to default ({DEFAULT_HARNESS})."
            )
            logger.info("Harness cleared for %s", key)
            return
        if choice not in VALID_HARNESSES:
            await update.message.reply_text(
                f"Invalid harness. Choose: {', '.join(VALID_HARNESSES)}, default"
            )
            return
        set_chat_harness(key, choice)
        await update.message.reply_text(
            f"Harness set to {choice}. Takes effect on next message."
        )
        logger.info("Harness set to %s for %s", choice, key)
        return

    current = get_chat_harness(key) or f"default ({DEFAULT_HARNESS})"
    await update.message.reply_text(
        f"Current harness: {current}\n"
        f"Valid choices: {', '.join(VALID_HARNESSES)}, default\n"
        f"Use /harness <name> to switch."
    )


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill the active Claude process / cancel the active turn for this chat/topic.

    Backend-agnostic via `_cancel_session_async`: cc-cli SIGKILLs the
    subprocess, cc-sdk task-cancels the harness across the worker loop.
    """
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    state = _sessions.get(key)
    if state is None or (state.proc is None and state.harness is None):
        await update.message.reply_text("No active Claude process in this chat.")
        return

    # Capture pid before cancel for logging — cc-sdk has no proc, log -1.
    proc = state.proc
    pid = proc.pid if proc is not None else -1
    harness_name = getattr(state.harness, "name", "cc-cli")

    await _cancel_session_async(state)
    await update.message.reply_text(
        "Killed active Claude process. Session preserved — next message resumes."
    )
    logger.info(
        "User %d killed Claude turn for %s (harness=%s, pid=%d)",
        user_id,
        key,
        harness_name,
        pid,
    )
    _log_activity(
        "process_kill",
        session_key=key,
        pid=pid,
        user_id=user_id,
        reason="manual",
        harness=harness_name,
    )


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart the bridge process. Launchd will respawn it."""
    user_id = update.effective_user.id
    await update.message.reply_text("Restarting bridge...")
    logger.info("User %d triggered bridge restart", user_id)

    # cc-cli paths: SIGTERM the proc (graceful, lets it write final stderr).
    for key, proc in _iter_active_procs():
        if proc.poll() is None:
            proc.terminate()
            logger.info("Terminated Claude process for %s (pid %d)", key, proc.pid)

    # cc-sdk paths: schedule a task cancel on the worker loop. Best-effort
    # — we're about to os._exit anyway, so any subprocess the SDK owns
    # dies as our child when we exit.
    for key, state in _iter_active_sessions():
        if state.proc is not None:
            continue  # already terminated above
        try:
            await _cancel_session_async(state)
        except Exception:  # noqa: BLE001
            logger.exception("Cancel for cc-sdk session %s during restart raised", key)

    if _remote_proc and _remote_proc.poll() is None:
        _remote_proc.terminate()
        logger.info("Terminated remote-control process (pid %d)", _remote_proc.pid)

    restart_notify = RESTART_NOTIFY_FILE
    restart_notify.write_text(
        json.dumps(
            {
                "chat_id": update.effective_chat.id,
                "thread_id": update.message.message_thread_id,
            }
        )
    )

    os._exit(1)


async def cmd_remote_control(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start or stop claude remote-control in this topic's project dir."""
    global _remote_proc, _remote_proc_key

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = (update.message.text or "").split()
    if len(args) > 1 and args[1].lower() == "stop":
        if _remote_proc and _remote_proc.poll() is None:
            _remote_proc.terminate()
            _remote_proc.wait(timeout=5)
            cwd_label = get_chat_working_dir(_remote_proc_key or key)
            _remote_proc = None
            _remote_proc_key = None
            await update.message.reply_text(f"Remote control stopped ({cwd_label})")
            logger.info("User %d stopped remote-control", user_id)
        else:
            _remote_proc = None
            _remote_proc_key = None
            await update.message.reply_text("No remote-control process running.")
        return

    if _remote_proc and _remote_proc.poll() is None:
        _remote_proc.terminate()
        try:
            _remote_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _remote_proc.kill()
        logger.info("Replaced existing remote-control process (pid %d)", _remote_proc.pid)

    chat_cwd = get_chat_working_dir(key)
    await update.message.reply_text(f"Starting remote-control in {chat_cwd}...")

    proc = subprocess.Popen(
        [CLAUDE_PATH, "remote-control"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=chat_cwd,
    )
    _remote_proc = proc
    _remote_proc_key = key
    logger.info("Started claude remote-control (pid %d) in %s", proc.pid, chat_cwd)
    _log_activity("remote_control_start", session_key=key, cwd=chat_cwd, pid=proc.pid)

    loop = asyncio.get_running_loop()

    def _read_initial_output() -> list[str]:
        collected = []
        deadline = time.time() + 10
        while time.time() < deadline:
            if proc.poll() is not None:
                remaining = proc.stdout.read()
                if remaining:
                    collected.extend(remaining.splitlines())
                break
            ready, _, _ = select.select([proc.stdout], [], [], 1.0)
            if ready:
                line = proc.stdout.readline()
                if line:
                    collected.append(line.rstrip())
        seen: set[str] = set()
        result: list[str] = []
        for raw in collected:
            clean = _ANSI_RE.sub("", raw).strip()
            if clean and clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result

    lines = await loop.run_in_executor(_executor, _read_initial_output)

    if proc.poll() is not None:
        output = "\n".join(lines) if lines else "(no output)"
        await update.message.reply_text(f"Remote control exited (code {proc.returncode}):\n{output}")
        _remote_proc = None
        _remote_proc_key = None
    else:
        # Spawn the background drainer now: nobody is reading stdout from
        # here on, and remote-control would deadlock on a full pipe (§17).
        _start_remote_drain(proc)
        output = "\n".join(lines) if lines else "(waiting for connection info...)"
        await update.message.reply_text(
            f"Remote control running (pid {proc.pid}):\n{output}\n\nUse /remote stop to shut it down."
        )


def _format_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _week_start_yyyymmdd() -> str:
    """Return the YYYYMMDD of the most recent Monday (or today if Monday)."""
    now = time.localtime()
    day_of_week = now.tm_wday  # 0 = Monday
    week_start = time.time() - day_of_week * 86400
    return time.strftime("%Y%m%d", time.localtime(week_start))


def _bar(pct: float, width: int = 12) -> str:
    """Render a percent as a unicode progress bar."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


def _block_time_percent(start_iso: str, end_iso: str) -> float | None:
    """Return percent of the 5h block elapsed (0..100), or None on parse error."""
    try:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    now = datetime.now(start.tzinfo)
    span = (end - start).total_seconds()
    if span <= 0:
        return None
    return max(0.0, min(100.0, (now - start).total_seconds() / span * 100))


def _week_time_percent() -> float:
    """Return percent of the current Mon→Mon week elapsed (local time)."""
    now = datetime.now()
    monday = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    next_mon = monday + timedelta(days=7)
    span = (next_mon - monday).total_seconds()
    return max(0.0, min(100.0, (now - monday).total_seconds() / span * 100))


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Claude Code quota: 5h block + week, each as a token-bar and time-bar.

    Max-plan focus: how much of the quota is consumed vs. how much of the
    period has elapsed. Costs are irrelevant on a flat-rate plan. The
    weekly cap is an estimate (USAGE_WEEKLY_TOKEN_CAP env var, default 3B)
    because Anthropic does not publish a weekly token cap — their weekly
    limits are expressed in hours of active session time, not tokens.
    """
    try:
        blocks_proc, weekly_proc = await asyncio.gather(
            asyncio.to_thread(
                subprocess.run,
                ["ccusage", "blocks", "--active", "--token-limit", "max", "--json"],
                capture_output=True,
                text=True,
                timeout=15,
            ),
            asyncio.to_thread(
                subprocess.run,
                ["ccusage", "weekly", "--json", "--since", _week_start_yyyymmdd()],
                capture_output=True,
                text=True,
                timeout=15,
            ),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        await update.message.reply_text(f"usage check failed: {e}")
        return

    lines: list[str] = []

    # --- Active 5h block ---
    try:
        blocks = json.loads(blocks_proc.stdout).get("blocks", [])
        if blocks:
            b = blocks[0]
            used = b.get("totalTokens", 0)
            tls = b.get("tokenLimitStatus") or {}
            limit = tls.get("limit")
            tok_pct = tls.get("percentUsed")
            time_pct = _block_time_percent(b.get("startTime", ""), b.get("endTime", ""))
            proj = b.get("projection") or {}
            remaining = int(proj.get("remainingMinutes", 0))
            hrs, mins = divmod(remaining, 60)
            lines.append("5h block:")
            if tok_pct is not None and limit:
                lines.append(
                    f"  tokens  {_bar(tok_pct)} {tok_pct:4.1f}%  ({_format_tokens(used)}/{_format_tokens(limit)})"
                )
            else:
                lines.append(f"  tokens  {_format_tokens(used)}")
            if time_pct is not None:
                lines.append(f"  time    {_bar(time_pct)} {time_pct:4.1f}%  ({hrs}h{mins:02d}m left)")
        else:
            lines.append("5h block: (none)")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        lines.append(f"blocks parse error: {e}")

    lines.append("")

    # --- This week ---
    try:
        weekly = json.loads(weekly_proc.stdout).get("weekly", [])
        w = weekly[-1] if weekly else None
        used = w.get("totalTokens", 0) if w else 0
        cap = USAGE_WEEKLY_TOKEN_CAP
        wk_tok_pct = used / cap * 100 if cap else 0
        wk_time_pct = _week_time_percent()
        lines.append(f"Week (cap {_format_tokens(cap)} est):")
        lines.append(f"  tokens  {_bar(wk_tok_pct)} {wk_tok_pct:4.1f}%  ({_format_tokens(used)}/{_format_tokens(cap)})")
        lines.append(f"  time    {_bar(wk_time_pct)} {wk_time_pct:4.1f}%")
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        lines.append(f"weekly parse error: {e}")

    await update.message.reply_text("\n".join(lines))


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    mins, _ = divmod(s, 60)
    if days:
        return f"{days}d{hours}h{mins:02d}m"
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def _format_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}{unit}"
        n /= 1024
    return f"{n:.1f}PiB"


def _safe_count(path: Path, pattern: str) -> int:
    """Count files matching pattern, or -1 on error (directory missing etc.)."""
    try:
        return sum(1 for _ in path.glob(pattern))
    except OSError as exc:
        logger.warning("Could not count %s/%s: %s", path, pattern, exc)
        return -1


async def cmd_health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report bridge liveness: uptime, queues, disk free, last error."""
    from patchbay.config import BASE_DIR

    now = time.time()
    uptime = _format_uptime(now - _BRIDGE_STARTED_AT)

    active = [k for k, s in _sessions.items() if s.processing]

    try:
        usage = shutil.disk_usage(BASE_DIR)
        disk_free = _format_bytes(usage.free)
        disk_line = f"disk free: {disk_free} ({100 * usage.free / usage.total:.0f}%)"
    except OSError as exc:
        logger.warning("disk_usage failed for %s: %s", BASE_DIR, exc)
        disk_line = "disk free: unknown (check logs)"

    session_count = _safe_count(SESSION_DIR, "*.json")
    pending_count = _safe_count(PENDING_DIR, "*.json")
    failed_dir = PENDING_DIR / "failed"
    failed_count = _safe_count(failed_dir, "*.json") if failed_dir.exists() else 0

    lines = [
        "bridge /health",
        f"uptime: {uptime}",
        f"active sessions: {len(active)}",
        f"session files on disk: {session_count}",
        f"pending messages: {pending_count}",
        f"failed pending (archived): {failed_count}",
        disk_line,
    ]
    if active:
        lines.append("active keys: " + ", ".join(sorted(active)))
    await update.message.reply_text("\n".join(lines))


async def cmd_activity(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the most recent activity.jsonl entries, optionally filtered by event.

    Usage:
      /activity                  - last 8 entries, any event
      /activity <event>          - last 8 entries matching <event> (substring)
      /activity <event> <count>  - last <count> matching entries (cap 25)

    Useful events to grep for:
      self_heal, claude_timeout, claude_error, markdown_send_failed,
      markdown_conversion_failed, message_dropped, process_kill, quota_hit
    """

    parts = (update.message.text or "").split(maxsplit=2)
    event_filter = parts[1] if len(parts) > 1 else None
    try:
        max_count = max(1, min(25, int(parts[2]))) if len(parts) > 2 else 8
    except ValueError:
        max_count = 8

    if not Path(ACTIVITY_LOG).exists():
        await update.message.reply_text("activity.jsonl does not exist yet.")
        return

    # Read tail of file (~last 200 lines is plenty even for max_count=25)
    try:
        with open(ACTIVITY_LOG) as f:
            lines = f.readlines()[-200:]
    except OSError as exc:
        await update.message.reply_text(f"Failed to read activity.jsonl: {exc}")
        return

    matches: list[dict] = []
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event_filter and event_filter not in entry.get("event", ""):
            continue
        matches.append(entry)
        if len(matches) >= max_count:
            break

    if not matches:
        suffix = f" matching {event_filter!r}" if event_filter else ""
        await update.message.reply_text(f"No activity entries{suffix} in the last 200 lines.")
        return

    out_lines = [
        f"activity (last {len(matches)}{', ' + event_filter if event_filter else ''}):",
    ]
    for entry in matches:
        ts = datetime.fromtimestamp(entry.get("ts", 0)).strftime("%m-%d %H:%M:%S")
        evt = entry.get("event", "?")
        # Compact one-line per entry; include up to ~3 informative fields.
        extras = []
        for k in ("session_key", "kind", "fixed", "error", "duration", "elapsed_ms",
                  "turns_used", "exit_code", "depth", "error_type", "actions"):
            if k in entry and entry[k] not in (None, "", []):
                v = entry[k]
                if isinstance(v, str) and len(v) > 80:
                    v = v[:77] + "…"
                extras.append(f"{k}={v}")
            if len(extras) >= 4:
                break
        out_lines.append(f"[{ts}] {evt}  {'  '.join(extras)}")
    await update.message.reply_text("\n".join(out_lines))


def _resolve_harness_for_inquiry(session_key: str):
    """Build a harness instance + TurnRequest suitable for one-shot inquiry
    methods (get_context, compact). Mirrors the dispatch in run_claude
    minus the proc-mirroring and per-turn callbacks. Returns
    (harness_name, harness, req) or None if the chat's harness doesn't
    exist or isn't suitable.
    """
    chat_cwd = get_chat_working_dir(session_key)
    session_id = get_session_id(session_key)
    model = get_chat_model(session_key)
    effort = resolve_effort(session_key)

    harness_name = get_chat_harness(session_key) or DEFAULT_HARNESS
    if harness_name not in VALID_HARNESSES:
        harness_name = DEFAULT_HARNESS

    if harness_name == "cc-sdk":
        harness = ClaudeSdkHarness(
            cli_path=CLAUDE_PATH, max_timeout_seconds=MAX_TIMEOUT
        )
    elif harness_name == "cc-cli":
        harness = ClaudeCliHarness(
            claude_path=CLAUDE_PATH, max_timeout_seconds=MAX_TIMEOUT
        )
    elif harness_name == "pi":
        from patchbay.harness import PiHarness
        harness = PiHarness(max_timeout_seconds=MAX_TIMEOUT)
    elif harness_name == "aider":
        from patchbay.harness import AiderHarness
        harness = AiderHarness(max_timeout_seconds=MAX_TIMEOUT)
    elif harness_name == "opencode":
        from patchbay.harness import OpenCodeHarness
        harness = OpenCodeHarness(max_timeout_seconds=MAX_TIMEOUT)
    else:
        return None

    req = TurnRequest(
        prompt="",  # /context and /compact set their own prompt
        session_key=session_key,
        project_dir=Path(chat_cwd),
        system_prompt="",
        resume_session_id=session_id,
        model=model,
        effort=effort,
        allowed_tools=None,
        disallowed_tools=None,
        max_turns=None,
        plugin_dir=None,
    )
    return harness_name, harness, req


def _fmt_tokens(n: int) -> str:
    """Render token counts as '12.3k' / '1.0M'."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


async def cmd_context(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current context-window usage for this chat's session.

    cc-sdk only today (cc-cli/pi/aider/opencode advertise
    supports_context_query=False). For unsupported harnesses, suggest
    /harness cc-sdk.
    """
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    resolved = _resolve_harness_for_inquiry(key)
    if resolved is None:
        await update.message.reply_text("No harness configured for this chat.")
        return
    harness_name, harness, req = resolved

    caps = CAPABILITIES_BY_NAME.get(harness_name)
    if caps is None or not caps.supports_context_query:
        await update.message.reply_text(
            f"/context isn't supported on {harness_name} yet. "
            f"Try /harness cc-sdk for this chat."
        )
        return

    try:
        usage = await harness.get_context(req)
    except Exception as exc:  # noqa: BLE001
        logger.exception("/context failed for %s", key)
        await update.message.reply_text(f"Couldn't read context: {exc}")
        return

    used = _fmt_tokens(usage.used_tokens)
    cap = _fmt_tokens(usage.max_tokens)
    pct = f"{usage.percentage:.0f}%"
    await update.message.reply_text(f"Context: {used} / {cap} ({pct})")


_SUMMARIZE_PROMPT = (
    "PATCHBAY COMPACT — produce a handoff summary of our conversation so "
    "far. Output ONLY the summary text, no preamble, no closing remark, "
    "no markdown wrapping. Cover: (1) the original goal and any sub-goals, "
    "(2) decisions made and why, (3) work in progress / what's next, "
    "(4) key file paths, commands, and gotchas, (5) anything I asked you "
    "to remember. Be thorough — this summary is the only memory carried "
    "into the next session. Override any standing 'be brief' instruction "
    "for THIS message only; handoff summaries must be complete."
)


def _build_handoff_prompt(summary: str) -> str:
    return (
        "[Carrying context forward from a compacted prior session.]\n\n"
        f"{summary.strip()}\n\n"
        "[End of carried-forward summary. Acknowledge briefly so we can "
        "continue from here.]"
    )


async def _fallback_compact(
    *,
    update: Update,
    session_key: str,
    instructions: str | None,
) -> None:
    """Generic /compact for harnesses that don't have a native one.

    Two-turn flow on the existing run_claude path:
      1. Run a synthesizer turn against the current session asking for a
         handoff summary.
      2. Clear the chat's session id (same as /clearnew).
      3. Run a handoff turn whose prompt IS the summary, in the new
         (now empty) session. The agent acknowledges; the new session
         carries the compacted context forward.

    Works on every harness because it only uses run_claude. Costs two
    turns instead of one in-place compaction, but doesn't require any
    per-harness implementation work.
    """
    loop = asyncio.get_running_loop()
    summarize_prompt = _SUMMARIZE_PROMPT
    if instructions:
        summarize_prompt += f"\n\nADDITIONAL FOCUS FROM USER: {instructions}"

    await update.message.reply_text("Compacting (fallback): summarizing prior session…")
    try:
        summary = await loop.run_in_executor(
            _executor, run_claude, summarize_prompt, session_key
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("/compact fallback summarize failed for %s", session_key)
        await update.message.reply_text(f"Compact failed during summarize: {exc}")
        return

    if not summary or summary.startswith(QUOTA_HIT_PREFIX):
        await update.message.reply_text(
            "Compact aborted — couldn't get a summary. Try again later."
        )
        return

    summary = summary.strip()
    summary_words = len(summary.split())

    clear_session(session_key)
    logger.info(
        "Compact fallback for %s: cleared session, summary=%d words",
        session_key, summary_words,
    )

    handoff_prompt = _build_handoff_prompt(summary)
    await update.message.reply_text(
        f"Started fresh session. Handing forward summary ({summary_words} words)…"
    )
    try:
        ack = await loop.run_in_executor(
            _executor, run_claude, handoff_prompt, session_key
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("/compact fallback handoff failed for %s", session_key)
        await update.message.reply_text(
            f"Summary captured but handoff failed: {exc}\n\n"
            "Your next message will start a fresh session without context."
        )
        return

    await update.message.reply_text(
        f"Compact done. Agent ack:\n\n{ack[:1500]}"
    )


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Compact the running context. Optional steering text after the command.

    Two paths:
    - Native (cc-sdk): pushes claude's `/compact` slash command into a
      transient SDK client and reports before/after token counts.
    - Fallback (every other harness): runs a summarizer turn, clears the
      session, then runs a handoff turn whose prompt IS the summary.
      Same semantics as `/clearnew` with the first message pre-loaded.
    """
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    parts = (update.message.text or "").split(maxsplit=1)
    instructions = parts[1].strip() if len(parts) > 1 else None

    resolved = _resolve_harness_for_inquiry(key)
    if resolved is None:
        await update.message.reply_text("No harness configured for this chat.")
        return
    harness_name, harness, req = resolved

    if not req.resume_session_id:
        await update.message.reply_text(
            "Nothing to compact — no active session in this chat. "
            "Send a message first to start one."
        )
        return

    caps = CAPABILITIES_BY_NAME.get(harness_name)
    if caps is not None and caps.supports_compact:
        # Native path.
        await update.message.reply_text("Compacting context…")
        try:
            result = await harness.compact(req, instructions=instructions)
        except Exception as exc:  # noqa: BLE001
            logger.exception("/compact native failed for %s", key)
            await update.message.reply_text(f"Compact failed: {exc}")
            return
        await update.message.reply_text(result.message)
        return

    # Fallback path: works on every harness via run_claude.
    await _fallback_compact(update=update, session_key=key, instructions=instructions)


async def cmd_soak(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the harness soak comparison and post the table.

    Usage:
      /soak               - all-time
      /soak 7d            - last 7 days (e.g. 24h, 30m, 3d)
      /soak 7d <session>  - filter to one session_key
    """

    parts = (update.message.text or "").split(maxsplit=2)
    since = parts[1] if len(parts) > 1 else None
    session = parts[2] if len(parts) > 2 else None

    cmd = ["uv", "run", "python", "scripts/harness_soak.py"]
    if since:
        cmd += ["--since", since]
    if session:
        cmd += ["--session", session]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(Path(__file__).parent),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, OSError) as exc:
        await update.message.reply_text(f"/soak failed: {exc}")
        return

    if proc.returncode != 0:
        err = (stderr or b"").decode(errors="replace")[:500]
        await update.message.reply_text(f"/soak exit {proc.returncode}: {err}")
        return

    out = (stdout or b"").decode(errors="replace").strip() or "(no output)"
    escaped = out.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    await update.message.reply_text(f"<pre>{escaped}</pre>", parse_mode="HTML")


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


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not any(s.processing for s in _sessions.values()):
        await update.message.reply_text("pong — no active sessions")
        return
    now = time.time()
    lines = ["pong — active sessions:"]
    for key in sorted(k for k, s in _sessions.items() if s.processing):
        started = _sessions[key].started_at if key in _sessions else None
        label = _session_display_label(key)
        if started:
            elapsed = int(now - started)
            mins, secs = divmod(elapsed, 60)
            lines.append(f"  {label}: running {mins}m{secs:02d}s")
        else:
            lines.append(f"  {label}: running (start time unknown)")
    await update.message.reply_text("\n".join(lines))


async def handle_forum_topic_event(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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

    Watches `state.last_event_at`, which the harness's `on_progress`
    callback (wired into `ClaudeCliHarness._drain_streams`) refreshes on
    every line of claude's JSON-mode output. A real hang — including a
    process blocked on a TCC dialog that nobody can click — produces
    zero events; the reader's timestamp stops advancing and we kill
    after STALL_TIMEOUT seconds of silence.
    """
    while True:
        await asyncio.sleep(STALL_POLL_INTERVAL)
        now = time.time()
        # Iterate by harness presence so cc-sdk turns are watched too.
        for key, state in _iter_active_sessions():
            proc = state.proc
            # cc-cli optimization: if the proc already exited, the harness
            # is in its wrap-up phase — clear the timer and move on.
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
        BotCommand("model", "Set model (opus/sonnet/haiku)"),
        BotCommand("effort", "Set effort level (low/medium/high/xhigh/max)"),
        BotCommand("remote_control", "Start/stop claude remote-control in project dir"),
        BotCommand("kill", "Kill active Claude process"),
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

    # cc-sdk sessions own their subprocess via the SDK; we can't reach
    # them from a sync signal handler. Log them so the operator knows
    # what's outstanding; the SDK's child process will receive SIGHUP /
    # see EOF on stdin once we exit and tear itself down.
    for key, state in _iter_active_sessions():
        if state.proc is not None:
            continue
        harness_name = getattr(state.harness, "name", "unknown")
        logger.info(
            "Active %s turn for %s during shutdown — relying on parent-exit cleanup",
            harness_name,
            key,
        )

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
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CallbackQueryHandler(callback_model, pattern=r"^model:"))
    app.add_handler(CommandHandler("effort", cmd_effort))
    app.add_handler(CommandHandler("harness", cmd_harness))
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
            filters.StatusUpdate.FORUM_TOPIC_CREATED
            | filters.StatusUpdate.FORUM_TOPIC_EDITED,
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
