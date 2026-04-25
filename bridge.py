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
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

import telegramify_markdown  # noqa: E402
from telegramify_markdown.customize import get_runtime_config  # noqa: E402
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.constants import ParseMode  # noqa: E402
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
from stargate.config import (  # noqa: E402
    ACTIVITY_LOG,  # noqa: F401 — used by tests via bridge.ACTIVITY_LOG
    ANSI_RE,
    BOT_TOKEN,
    CHAT_PROJECTS_FILE,
    CLAUDE_PATH,
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
    STALL_CPU_THRESHOLD,
    STALL_POLL_INTERVAL,
    STALL_TIMEOUT,
    TELEGRAM_MSG_LIMIT,
    TYPING_INTERVAL,
    USAGE_WEEKLY_TOKEN_CAP,
    WORKING_DIR,
    logger,
)
from stargate.sessions import (  # noqa: E402
    _sanitize_session_key,  # noqa: F401 — used by tests via bridge._sanitize_session_key
    _session_key,
    clear_pending,
    clear_session,
    consume_stall_kill,
    get_session_id,
    mark_stall_kill,
    save_pending,
    save_session_id,  # noqa: F401 — used by tests via bridge.save_session_id
)
from stargate.parser import (  # noqa: E402
    _parse_events,
    parse_claude_response,
)
from stargate.quota import (  # noqa: E402
    handoff_to_forge as _handoff_to_forge_impl,
    is_quota_error as _is_quota_error_impl,
)
from stargate.activity import log_activity  # noqa: E402
from stargate.outbound import get_recent_outbound, log_outbound_response  # noqa: E402
from stargate.models import (  # noqa: E402
    VALID_MODELS,
    extract_model_prefix,
    get_chat_model,
    set_chat_model,
)
from stargate.efforts import (  # noqa: E402
    DEFAULT_EFFORT,
    VALID_EFFORTS,
    get_chat_effort,
    resolve_effort,
    set_chat_effort,
)
from stargate.projects import (  # noqa: E402
    _load_chat_projects,
    _parse_project_entry,
    get_all_projects as _get_all_projects,
    get_chat_agent,
    get_chat_working_dir,
    set_chat_project,
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
# Per-session state (CTB-ucw)
#
# Consolidates what used to be six parallel dicts keyed by session_key into a
# single SessionState object. The legacy module-level dicts below remain as
# the live data store during the migration; each subsequent commit will move
# one field from the legacy dicts onto SessionState until they can all be
# deleted. New code should read/write through `_get_session_state(key)`.
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    """All per-session runtime state, keyed by session_key in `_sessions`."""

    proc: subprocess.Popen | None = None
    started_at: float | None = None  # time.time() when processing began
    last_event_at: float | None = None  # last time stall-detector saw activity
    queue: list[str] = field(default_factory=list)  # debounced messages awaiting processing
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
    """Snapshot of (session_key, proc) for every session with a live subprocess."""
    return [(k, s.proc) for k, s in _sessions.items() if s.proc is not None]


# Track remote-control process (only one at a time, keyed by session key)
_remote_proc: subprocess.Popen | None = None
_remote_proc_key: str | None = None

# Message debounce: batch messages that arrive while Claude is processing
_processing_sessions: set[str] = set()
_queued_messages: dict[str, list[str]] = {}


# Flag to block new messages during graceful shutdown
_shutting_down = False

# Bot instance (set in post_init)
_bot_instance = None


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def run_claude(message: str, session_key: str, _retry: bool = False, model: str | None = None) -> str:
    """Invoke claude CLI via Popen. Does not kill on timeout."""
    session_id = get_session_id(session_key)
    chat_cwd = get_chat_working_dir(session_key)

    projects = _load_chat_projects()
    entry = projects.get(session_key)
    rel_path, _ = _parse_project_entry(entry)
    project_info = f"~/Developer/{rel_path}" if rel_path else "~/Developer (default)"

    agent_name = get_chat_agent(session_key)

    system_prompt = (
        "Bryan is messaging you via Telegram from his phone. Keep responses concise - he's on mobile. "
        "You have full access to all your MCP tools and can do real work. "
        "For email access, use the himalaya CLI: 'himalaya envelope list --account icloud' or '--account gmail' to list emails, "
        "'himalaya message read <id> --account <account>' to read them. "
        "IMPORTANT: NEVER use the AskUserQuestion tool - it requires interactive terminal UI that doesn't work through Telegram. "
        "Instead, ask questions as plain text in your response and let Bryan reply naturally.\n\n"
        f"TURN LIMIT: This session has a {MAX_TURNS}-turn limit. If a task will take more than ~20 tool calls, "
        "decompose it: do the critical/unblocking work now, create beads for the remaining subtasks, "
        "then report what you did and what's queued. Don't get cut off mid-task.\n\n"
        "CRITICAL: Your FINAL output MUST be a text response to Adrien — never end on a tool call. "
        "If you've done work via tools, summarize what you did in a short text message at the end. "
        "If you don't produce text, Adrien sees '(no parseable response)' which is a failure.\n\n"
        "TOOL STDOUT IS INVISIBLE TO ADRIEN: Anything printed by Bash, Python scripts, or other tools "
        "goes to YOUR context only — never to Telegram. If you run a script that prints prototypes, "
        "tables, mockups, or any content you want Adrien to see, you MUST inline that content verbatim "
        "in your text response. Never write 'three shapes above', 'see output', 'here's the result', "
        "or any phrase that implies Adrien can see what the tool printed. If the tool output is what "
        "you're showing him, paste it into your text message. This failure mode has bitten us before — "
        "when in doubt, inline it.\n\n"
        "FORMATTING: Telegram renders your replies as MarkdownV2 (converted from standard markdown by the bridge). "
        "Use normal markdown — `inline code`, ```code blocks```, **bold**, *italic*, bullet lists, and block quotes all render. "
        "Tables are NOT supported by Telegram and will be rendered as a plain code block, so prefer bullet lists or "
        "ASCII-aligned columns inside a ``` code block for tabular data.\n\n"
        "HEADLESS-ONLY — READ THIS CAREFULLY: You are running as a launchd LaunchAgent on a Mac Mini "
        "that Bryan is NOT sitting in front of. He is on his phone via Telegram. Any command that "
        "requires GUI interaction, macOS TCC permission dialogs, or Accessibility/Screen Recording "
        "access will silently hang you for 30 minutes until the stall detector kills the process. "
        "NEVER run these: XCUITest, `xcodebuild test` with UI test targets, `tuist test` with UI tests, "
        "`open -a`, `osascript` targeting GUI apps you haven't pre-approved, Simulator boot/launch, "
        "Instruments, Accessibility Inspector, anything requiring Screen Recording. If a task truly "
        "requires one of these, STOP and tell Bryan — don't try to run it. Prefer `swift test`, unit "
        "tests only, `tuist build` over `tuist test`, and static analysis/grep over runtime inspection. "
        "When in doubt whether a command is headless-safe, ask before running it.\n\n"
        "SHARED FILES: There is a ProtonDrive folder synced to this machine. "
        "Find it at ~/Library/CloudStorage/ProtonDrive-*/Claude-Support (glob for the exact path). "
        "You can drop files there (documents, images, exports) for Bryan to access from any device. "
        "Photos sent from Telegram are already handled separately via the photo handler.\n\n"
        f"Telegram session key: {session_key}\n"
        f"Working directory: {project_info}\n"
        f"Chat projects config: {CHAT_PROJECTS_FILE}\n"
        "You can change your own project directory by editing chat_projects.json "
        "(map session key to a path relative to ~/Developer). "
        "After changing it, tell Bryan to run /clearnew to pick up the new cwd."
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
            "Simulator boot, Instruments, etc.). Bryan is on his phone — he cannot click the dialog. "
            "Look at your last tool call in the conversation history, DO NOT RETRY IT, and pick a "
            "headless-safe alternative (swift test, tuist build, unit tests, static analysis)."
        )

    cmd = [
        CLAUDE_PATH,
        "-p",
        message,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--disallowed-tools",
        "AskUserQuestion,EnterPlanMode,ExitPlanMode",
        "--max-turns",
        str(MAX_TURNS),
        "--plugin-dir",
        PA_PLUGIN_DIR,
        "--append-system-prompt",
        system_prompt,
    ]

    # Resolve model: per-message override > sticky setting > default
    if not model:
        model = get_chat_model(session_key)
    if model:
        cmd.extend(["--model", model])

    # Resolve effort: per-chat sticky setting > DEFAULT_EFFORT
    effort = resolve_effort(session_key)
    cmd.extend(["--effort", effort])

    if session_id:
        cmd.extend(["--resume", session_id])
        logger.info("Resuming session %s for %s", session_id[:12], session_key)

    logger.info(
        "Launching claude in %s for %s (model=%s, effort=%s)",
        chat_cwd,
        session_key,
        model or "default",
        effort,
    )
    invoke_start = time.time()
    _log_activity(
        "claude_invoke",
        session_key=session_key,
        cwd=chat_cwd,
        model=model or "default",
        effort=effort,
        resume=bool(session_id),
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=chat_cwd,
    )
    state = _get_session_state(session_key)
    state.proc = proc
    state.last_event_at = time.time()

    try:
        stdout, stderr = proc.communicate(timeout=MAX_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # drain pipes
        duration = time.time() - invoke_start
        _log_activity("claude_timeout", session_key=session_key, pid=proc.pid, duration=duration)
        return f"[Timed out after {MAX_TIMEOUT // 60} min] Session preserved — send your message again to resume."
    finally:
        state = _sessions.get(session_key)
        if state is not None:
            state.proc = None
            state.last_event_at = None

    duration = time.time() - invoke_start
    stderr = stderr or ""
    stdout = stdout.strip()
    if not stdout:
        # Stale session: Claude couldn't find the conversation. Clear and retry.
        if stderr and "No conversation found" in stderr and session_id and not _retry:
            logger.warning("Stale session %s for %s, retrying fresh", session_id[:12], session_key)
            clear_session(session_key)
            return run_claude(message, session_key, _retry=True)
        # Check for quota error in stderr even when stdout is empty
        if _is_quota_error([], stderr):
            logger.warning(
                "Quota/rate limit detected (no output) for %s: %s",
                session_key,
                stderr[:200],
            )
            _log_activity("quota_hit", session_key=session_key, duration=duration, source="stderr")
            return QUOTA_HIT_PREFIX + message
        _log_activity(
            "claude_error",
            session_key=session_key,
            duration=duration,
            error=stderr[:200] if stderr else "no output",
        )
        if stderr:
            return f"(no output. stderr: {stderr[:500]})"
        return "(no output)"

    _log_activity(
        "claude_complete",
        session_key=session_key,
        duration=duration,
        exit_code=proc.returncode,
        response_len=len(stdout),
    )
    if proc.returncode != 0 and stderr:
        logger.warning(
            "Claude exited %d for %s. stderr: %s",
            proc.returncode,
            session_key,
            stderr[:300],
        )

    # Check for quota error in completed response
    events = _parse_events(stdout)
    if _is_quota_error(events, stderr):
        logger.warning("Quota/rate limit detected for %s", session_key)
        _log_activity(
            "quota_hit",
            session_key=session_key,
            duration=duration,
            source="events+stderr",
        )
        parse_claude_response(stdout, session_key)  # side-effect: saves session_id
        return QUOTA_HIT_PREFIX + message

    response = parse_claude_response(stdout, session_key)

    # If parsing produced nothing useful and Claude errored, surface stderr
    if response == "(no parseable response)" and proc.returncode != 0 and stderr:
        return f"(Claude exited with error: {stderr[:500]})"
    return response


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


async def keep_typing(
    chat_id: int,
    thread_id: int | None,
    stop_event: asyncio.Event,
    bot,
) -> None:
    """Send typing indicator every few seconds until stop_event is set."""
    kwargs = {"chat_id": chat_id, "action": "typing"}
    if thread_id is not None:
        kwargs["message_thread_id"] = thread_id
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(**kwargs)
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TYPING_INTERVAL)
            return
        except asyncio.TimeoutError:
            continue


def _to_markdownv2(text: str) -> str | None:
    """Convert markdown to Telegram MarkdownV2. Returns None on failure."""
    try:
        return telegramify_markdown.markdownify(text)
    except Exception:
        return None


async def _send_response(bot, chat_id: int, thread_id: int | None, response: str) -> None:
    """Send a response, splitting at Telegram's message limit.

    Each chunk is sent with MarkdownV2 formatting when possible, falling
    back to plain text if conversion or rendering fails. Retries each
    chunk up to SEND_RETRY_ATTEMPTS times with exponential backoff.

    Every send attempt's outcome is recorded to the outbound audit log
    (source="claude-response") for diagnosing client-side render drops
    — see CTB-80f. Audit failures are swallowed: they must never affect
    user-visible send behavior.
    """
    send_kwargs: dict = {"chat_id": chat_id}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id
    audit_session_key = _session_key(chat_id, thread_id)
    chunk_total = max(1, (len(response) + TELEGRAM_MSG_LIMIT - 1) // TELEGRAM_MSG_LIMIT)
    chunk_index = -1
    for i in range(0, len(response), TELEGRAM_MSG_LIMIT):
        chunk_index += 1
        chunk = response[i : i + TELEGRAM_MSG_LIMIT]
        md_chunk = _to_markdownv2(chunk)
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
                # If the markdown variant failed (likely malformed entities),
                # drop markdown and retry the remaining attempts as plain text.
                if md_chunk is not None:
                    logger.debug("MarkdownV2 send failed, falling back to plain: %s", e)
                    md_chunk = None
                delay = SEND_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "Telegram send failed (attempt %d/%d) for chat=%s thread=%s chunk_start=%d: %s — retrying in %.1fs",
                    attempt + 1,
                    SEND_RETRY_ATTEMPTS,
                    chat_id,
                    thread_id,
                    i,
                    e,
                    delay,
                )
                if attempt < SEND_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(delay)
        if last_exc is not None:
            logger.error(
                "Telegram send failed after %d attempts for chat=%s thread=%s chunk_start=%d: %s",
                SEND_RETRY_ATTEMPTS,
                chat_id,
                thread_id,
                i,
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

        logger.info("Replaying message for %s: %s", session_key, text[:80])

        # Delete pending file BEFORE processing to prevent crash loops.
        f.unlink()

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(keep_typing(chat_id, thread_id, stop_typing, bot))

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(_executor, run_claude, text, session_key)
        except Exception as e:
            logger.error("Error replaying %s: %s", f.stem, e)
            response = (
                f"Error replaying your message after bridge restart.\n"
                f"Original message: {text[:200]}\n"
                f"Error: {type(e).__name__}: {e}"
            )
        finally:
            stop_typing.set()
            await typing_task

        full_response = "[Recovered after bridge restart]\n\n" + response

        send_kwargs = {"chat_id": chat_id}
        if thread_id is not None:
            send_kwargs["message_thread_id"] = thread_id

        for i in range(0, len(full_response), TELEGRAM_MSG_LIMIT):
            await bot.send_message(text=full_response[i : i + TELEGRAM_MSG_LIMIT], **send_kwargs)

        logger.info("Replayed pending message %s", f.stem)


# ---------------------------------------------------------------------------
# Message and photo handlers
# ---------------------------------------------------------------------------


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

    # Debounce: if Claude is already processing for this session, queue the message
    if key in _processing_sessions:
        queue = _queued_messages.setdefault(key, [])
        if len(queue) >= MAX_QUEUED_MESSAGES:
            await update.message.reply_text(
                f"Queue full ({MAX_QUEUED_MESSAGES}) — message dropped. Wait for current response to finish."
            )
            logger.warning("Queue full for %s, dropping message", key)
            _log_activity("message_dropped", session_key=key, depth=len(queue))
            return
        queue.append(text)
        depth = len(queue)
        await update.message.reply_text(f"Queued ({depth}) — will send when current response finishes.")
        logger.info("Queued message for %s (depth: %d)", key, depth)
        _log_activity("message_queued", session_key=key, depth=depth)
        return

    # Check for per-message model prefix (e.g. "!sonnet do something")
    msg_model, clean_text = extract_model_prefix(text)

    _processing_sessions.add(key)
    _get_session_state(key).started_at = time.time()
    pending_id = save_pending(chat_id, thread_id, clean_text, key)

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, thread_id, stop_typing, context.bot))

    try:
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(_executor, lambda: run_claude(clean_text, key, model=msg_model))
        except Exception as e:
            logger.error("Error running claude for %s: %s", key, e)
            response = f"Error: {e}"

        # Quota hit — hand off to Forge instead of sending error to user
        if response.startswith(QUOTA_HIT_PREFIX):
            original_msg = response[len(QUOTA_HIT_PREFIX) :]
            session_id = get_session_id(key)
            chat_cwd = get_chat_working_dir(key)
            handed_off = _handoff_to_forge(
                session_key=key,
                message=original_msg,
                chat_id=chat_id,
                thread_id=thread_id,
                session_id=session_id,
                working_dir=chat_cwd,
            )
            if handed_off:
                response = (
                    "Hit a quota/rate limit. Handed this off to Forge — "
                    "it'll pick up where this left off and send the response "
                    "back here when done."
                )
            else:
                response = (
                    "Hit a quota/rate limit. Tried to hand off to Forge but "
                    "failed to write the queue file. Try again later."
                )

        try:
            await _send_response(context.bot, chat_id, thread_id, response)
            clear_pending(pending_id)
        except Exception as e:
            logger.error("Failed to send response for %s: %s", key, e)
            await _notify_delivery_failure(context.bot, chat_id, thread_id, key)

        # Drain queued messages: batch all into a single Claude invocation
        while _queued_messages.get(key):
            batch = _queued_messages.pop(key)
            logger.info("Processing %d queued message(s) for %s", len(batch), key)
            if len(batch) == 1:
                combined = batch[0]
            else:
                combined = "\n\n---\n\n".join(f"[Follow-up {i + 1}]\n{msg}" for i, msg in enumerate(batch))
            try:
                response = await loop.run_in_executor(_executor, run_claude, combined, key)
            except Exception as e:
                logger.error("Error running claude for queued batch %s: %s", key, e)
                response = f"Error: {e}"
            try:
                await _send_response(context.bot, chat_id, thread_id, response)
            except Exception as e:
                logger.error("Failed to send queued response for %s: %s", key, e)
                await _notify_delivery_failure(context.bot, chat_id, thread_id, key)
    finally:
        stop_typing.set()
        await typing_task
        _processing_sessions.discard(key)
        _sessions[key].started_at = None  # type: ignore[union-attr]


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

    # Debounce: if Claude is already processing for this session, queue the photo prompt
    if key in _processing_sessions:
        queue = _queued_messages.setdefault(key, [])
        if len(queue) >= MAX_QUEUED_MESSAGES:
            await update.message.reply_text(
                f"Queue full ({MAX_QUEUED_MESSAGES}) — photo dropped. Wait for current response to finish."
            )
            logger.warning("Queue full for %s, dropping photo", key)
            _log_activity("photo_dropped", session_key=key, depth=len(queue))
            local_path.unlink(missing_ok=True)
            return
        queue.append(prompt)
        depth = len(queue)
        await update.message.reply_text(f"Photo queued ({depth}) — will send when current response finishes.")
        logger.info("Queued photo for %s (depth: %d)", key, depth)
        _log_activity("photo_queued", session_key=key, depth=depth)
        return

    _processing_sessions.add(key)
    _get_session_state(key).started_at = time.time()
    pending_id = save_pending(chat_id, thread_id, prompt, key)

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, thread_id, stop_typing, context.bot))

    try:
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(_executor, run_claude, prompt, key)
        except Exception as e:
            logger.error("Error running claude for photo %s: %s", key, e)
            response = f"Error: {e}"

        try:
            await _send_response(context.bot, chat_id, thread_id, response)
            clear_pending(pending_id)
        except Exception as e:
            logger.error("Failed to send photo response for %s: %s", key, e)
            await _notify_delivery_failure(context.bot, chat_id, thread_id, key)

        # Drain queued messages (same as handle_message)
        while _queued_messages.get(key):
            batch = _queued_messages.pop(key)
            logger.info("Processing %d queued message(s) for %s", len(batch), key)
            if len(batch) == 1:
                combined = batch[0]
            else:
                combined = "\n\n---\n\n".join(f"[Follow-up {i + 1}]\n{msg}" for i, msg in enumerate(batch))
            try:
                response = await loop.run_in_executor(_executor, run_claude, combined, key)
            except Exception as e:
                logger.error("Error running claude for queued batch %s: %s", key, e)
                response = f"Error: {e}"
            try:
                await _send_response(context.bot, chat_id, thread_id, response)
            except Exception as e:
                logger.error("Failed to send queued response for %s: %s", key, e)
                await _notify_delivery_failure(context.bot, chat_id, thread_id, key)
    finally:
        stop_typing.set()
        await typing_task
        _processing_sessions.discard(key)
        _sessions[key].started_at = None  # type: ignore[union-attr]
        local_path.unlink(missing_ok=True)


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

    # Debounce: if Claude is already processing for this session, queue the prompt
    if key in _processing_sessions:
        queue = _queued_messages.setdefault(key, [])
        if len(queue) >= MAX_QUEUED_MESSAGES:
            await update.message.reply_text(
                f"Queue full ({MAX_QUEUED_MESSAGES}) — file dropped. Wait for current response to finish."
            )
            logger.warning("Queue full for %s, dropping document", key)
            _log_activity("document_dropped", session_key=key, depth=len(queue))
            local_path.unlink(missing_ok=True)
            return
        queue.append(prompt)
        depth = len(queue)
        await update.message.reply_text(f"File queued ({depth}) — will process when current response finishes.")
        logger.info("Queued document for %s (depth: %d)", key, depth)
        _log_activity("document_queued", session_key=key, depth=depth)
        return

    _processing_sessions.add(key)
    _get_session_state(key).started_at = time.time()
    pending_id = save_pending(chat_id, thread_id, prompt, key)

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(chat_id, thread_id, stop_typing, context.bot))

    try:
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(_executor, run_claude, prompt, key)
        except Exception as e:
            logger.error("Error running claude for document %s: %s", key, e)
            response = f"Error: {e}"

        try:
            await _send_response(context.bot, chat_id, thread_id, response)
            clear_pending(pending_id)
        except Exception as e:
            logger.error("Failed to send document response for %s: %s", key, e)
            await _notify_delivery_failure(context.bot, chat_id, thread_id, key)

        # Drain queued messages
        while _queued_messages.get(key):
            batch = _queued_messages.pop(key)
            logger.info("Processing %d queued message(s) for %s", len(batch), key)
            if len(batch) == 1:
                combined = batch[0]
            else:
                combined = "\n\n---\n\n".join(f"[Follow-up {i + 1}]\n{msg}" for i, msg in enumerate(batch))
            try:
                response = await loop.run_in_executor(_executor, run_claude, combined, key)
            except Exception as e:
                logger.error("Error running claude for queued batch %s: %s", key, e)
                response = f"Error: {e}"
            try:
                await _send_response(context.bot, chat_id, thread_id, response)
            except Exception as e:
                logger.error("Failed to send queued response for %s: %s", key, e)
                await _notify_delivery_failure(context.bot, chat_id, thread_id, key)
    finally:
        stop_typing.set()
        await typing_task
        _processing_sessions.discard(key)
        _sessions[key].started_at = None  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Stargate active.\nYour Telegram user ID: {uid}\n\n"
        "Commands:\n"
        "/clearnew - Start a fresh conversation (in current topic)\n"
        "/setproject <path> - Set project dir (relative to ~/Developer)\n"
        "/setproject - Clear project binding (use default)\n"
        "/project - Show current project dir\n"
        "/model - Set model (opus/sonnet/haiku) or prefix with !s !o !h\n"
        "/effort - Set effort level (low/medium/high/xhigh/max)\n"
        "/remote-control - Start claude remote-control in this topic's project dir\n"
        "/remote-control stop - Stop remote-control\n"
        "/kill - Kill active Claude process\n"
        "/restart - Restart the bridge\n"
        "/ping - Check if bridge is alive\n"
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


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill the active Claude process for this chat/topic."""
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    state = _sessions.get(key)
    proc = state.proc if state else None
    if proc and proc.poll() is None:
        proc.kill()
        await update.message.reply_text("Killed active Claude process. Session preserved — next message resumes.")
        logger.info("User %d killed Claude process for %s (pid %d)", user_id, key, proc.pid)
        _log_activity(
            "process_kill",
            session_key=key,
            pid=proc.pid,
            user_id=user_id,
            reason="manual",
        )
    else:
        await update.message.reply_text("No active Claude process in this chat.")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart the bridge process. Launchd will respawn it."""
    user_id = update.effective_user.id
    await update.message.reply_text("Restarting bridge...")
    logger.info("User %d triggered bridge restart", user_id)

    for key, proc in _iter_active_procs():
        if proc.poll() is None:
            proc.terminate()
            logger.info("Terminated Claude process for %s (pid %d)", key, proc.pid)

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


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _processing_sessions:
        await update.message.reply_text("pong — no active sessions")
        return
    now = time.time()
    lines = ["pong — active sessions:"]
    for key in sorted(_processing_sessions):
        started = (_sessions[key].started_at if key in _sessions else None)
        if started:
            elapsed = int(now - started)
            mins, secs = divmod(elapsed, 60)
            lines.append(f"  {key}: running {mins}m{secs:02d}s")
        else:
            lines.append(f"  {key}: running (start time unknown)")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Infrastructure: stall detector, lifecycle
# ---------------------------------------------------------------------------


def _get_proc_cpu(pid: int) -> float | None:
    """Get %CPU for a process via ps. Returns None if process not found."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "%cpu="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError):
        pass
    return None


async def _stall_detector() -> None:
    """Background task: poll active Claude processes for CPU stalls."""
    while True:
        await asyncio.sleep(STALL_POLL_INTERVAL)
        now = time.time()
        for key, proc in _iter_active_procs():
            state = _sessions[key]
            if proc.poll() is not None:
                state.last_event_at = None
                continue
            cpu = _get_proc_cpu(proc.pid)
            if cpu is None:
                continue
            if cpu >= STALL_CPU_THRESHOLD:
                state.last_event_at = now
                continue
            if state.last_event_at is None:
                state.last_event_at = now
                continue
            last_active = state.last_event_at
            stall_duration = now - last_active
            if stall_duration >= STALL_TIMEOUT:
                logger.warning(
                    "Killing stalled Claude process for %s (pid %d, idle %.0fs)",
                    key,
                    proc.pid,
                    stall_duration,
                )
                proc.kill()
                state.last_event_at = None
                try:
                    mark_stall_kill(key, stall_duration / 60)
                except ValueError:
                    pass  # invalid session key format — skip marker
                _log_activity(
                    "process_kill",
                    session_key=key,
                    pid=proc.pid,
                    reason="stalled",
                    idle_seconds=stall_duration,
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
        except Exception:
            pass

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
    ]
    await app.bot.set_my_commands(commands)
    logger.info("Bot commands registered with Telegram")

    asyncio.create_task(_stall_detector())
    logger.info(
        "Stall detector started (poll=%ds, timeout=%ds)",
        STALL_POLL_INTERVAL,
        STALL_TIMEOUT,
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
        except Exception:
            pass
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
    except Exception:
        pass

    logger.info("Graceful shutdown complete — exiting")
    sys.exit(0)


def main() -> None:
    # Single-instance guard FIRST — before any Telegram polling starts.
    # Prevents two bridges racing on getUpdates (409 storm, CTB-72m).
    from stargate.log_filters import install_filters
    from stargate.singleton import acquire_singleton

    acquire_singleton()
    install_filters()

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
    app.add_handler(CallbackQueryHandler(callback_effort, pattern=r"^effort:"))
    app.add_handler(CommandHandler("remote_control", cmd_remote_control))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("usage", cmd_usage))
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
