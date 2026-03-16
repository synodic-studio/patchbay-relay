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
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update  # noqa: E402
from telegram.ext import (  # noqa: E402
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import auth  # noqa: E402

# ---------------------------------------------------------------------------
# Import from package modules — these are the canonical implementations.
# Re-export at module level for backward compatibility with existing tests
# and validate.py.
# ---------------------------------------------------------------------------
from stargate.config import (  # noqa: E402
    ACTIVITY_LOG,  # noqa: F401 — used by tests via bridge.ACTIVITY_LOG
    ALLOWED_USER_IDS,
    ANSI_RE,
    AUTH_BASE_URL,
    AUTH_REQUIRED,
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
    WORKING_DIR,
    logger,
)
from stargate.sessions import (  # noqa: E402
    _sanitize_session_key,  # noqa: F401 — used by tests via bridge._sanitize_session_key
    _session_key,
    clear_pending,
    clear_session,
    get_session_id,
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

# Track active Claude subprocesses per session key so /kill can terminate them
_active_procs: dict[str, subprocess.Popen] = {}
# Track remote-control process (only one at a time, keyed by session key)
_remote_proc: subprocess.Popen | None = None
_remote_proc_key: str | None = None

# Message debounce: batch messages that arrive while Claude is processing
_processing_sessions: set[str] = set()
_session_start_times: dict[
    str, float
] = {}  # session_key -> time.time() when processing began
_queued_messages: dict[str, list[str]] = {}

# Stalled process detector
_proc_last_active: dict[
    str, float
] = {}  # session_key -> last time CPU was above threshold

# Flag to block new messages during graceful shutdown
_shutting_down = False

# Bot instance (set in post_init)
_bot_instance = None


# ---------------------------------------------------------------------------
# Claude invocation
# ---------------------------------------------------------------------------


def run_claude(message: str, session_key: str, _retry: bool = False) -> str:
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
        "Bryan's accounts: iCloud (REDACTED@example.com) and Gmail (REDACTED@example.com). "
        "IMPORTANT: NEVER use the AskUserQuestion tool - it requires interactive terminal UI that doesn't work through Telegram. "
        "Instead, ask questions as plain text in your response and let Bryan reply naturally.\n\n"
        f"TURN LIMIT: This session has a {MAX_TURNS}-turn limit. If a task will take more than ~20 tool calls, "
        "decompose it: do the critical/unblocking work now, create beads for the remaining subtasks, "
        "then report what you did and what's queued. Don't get cut off mid-task.\n\n"
        "FORMATTING: Telegram renders messages as plain text — no markdown. "
        "For any tabular or structured data, use ASCII art (aligned columns, dashes, box-drawing characters).\n\n"
        "SHARED FILES: Bryan has a ProtonDrive folder synced to this machine at "
        "~/Library/CloudStorage/ProtonDrive-REDACTED@example.com-folder/Claude-Support. "
        "You can drop files there (documents, images, exports) for Bryan to access from any device. "
        "Photos sent from Telegram are already handled separately via the photo handler.\n\n"
        f"Telegram session key: {session_key}\n"
        f"Working directory: {project_info}\n"
        f"Chat projects config: {CHAT_PROJECTS_FILE}\n"
        "You can change your own project directory by editing chat_projects.json "
        "(map session key to a path relative to ~/Developer). "
        "After changing it, tell Bryan to run /clearnew to pick up the new cwd."
    )

    if agent_name:
        system_prompt += (
            f"\n\nAGENT MODE: You are the '{agent_name}' agent from Fanta. "
            f"On session start, read your identity stack in this order:\n"
            f"1. USER.md (global)\n"
            f"2. TOOLS.md (global)\n"
            f"3. Find your agent directory (search agents/dev/{agent_name}/, agents/pa/{agent_name}/, or agents/{agent_name}/) and read:\n"
            f"   - SOUL.md\n"
            f"   - IDENTITY.md (if it exists)\n"
            f"   - AGENTS.md\n"
            f"   - HEARTBEAT.md (if it exists)\n"
            f"Do NOT read other agents' files. You are ONLY the {agent_name} agent. "
            f"Adopt the personality and boundaries defined in your SOUL.md. "
            f"Skip MEMORY.md in Telegram context (per Fanta conventions)."
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

    if session_id:
        cmd.extend(["--resume", session_id])
        logger.info("Resuming session %s for %s", session_id[:12], session_key)

    logger.info("Launching claude in %s for %s", chat_cwd, session_key)
    invoke_start = time.time()
    _log_activity(
        "claude_invoke",
        session_key=session_key,
        cwd=chat_cwd,
        model="default",
        resume=bool(session_id),
    )

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=chat_cwd,
    )
    _active_procs[session_key] = proc
    _proc_last_active[session_key] = time.time()

    try:
        stdout, stderr = proc.communicate(timeout=MAX_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()  # drain pipes
        duration = time.time() - invoke_start
        _log_activity(
            "claude_timeout", session_key=session_key, pid=proc.pid, duration=duration
        )
        return f"[Timed out after {MAX_TIMEOUT // 60} min] Session preserved — send your message again to resume."
    finally:
        _active_procs.pop(session_key, None)
        _proc_last_active.pop(session_key, None)

    duration = time.time() - invoke_start
    stderr = stderr or ""
    stdout = stdout.strip()
    if not stdout:
        # Stale session: Claude couldn't find the conversation. Clear and retry.
        if stderr and "No conversation found" in stderr and session_id and not _retry:
            logger.warning(
                "Stale session %s for %s, retrying fresh", session_id[:12], session_key
            )
            clear_session(session_key)
            return run_claude(message, session_key, _retry=True)
        # Check for quota error in stderr even when stdout is empty
        if _is_quota_error([], stderr):
            logger.warning(
                "Quota/rate limit detected (no output) for %s: %s",
                session_key,
                stderr[:200],
            )
            _log_activity(
                "quota_hit", session_key=session_key, duration=duration, source="stderr"
            )
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


async def _check_auth(update: Update) -> bool:
    """Check if user is authenticated. Returns True if authorized to proceed."""
    if not AUTH_REQUIRED:
        return True
    user_id = update.effective_user.id
    if not auth.is_authenticated(user_id):
        return False
    auth.touch_session(user_id)
    return True


async def _send_auth_link(update: Update) -> None:
    """Generate and send an auth link to the user."""
    user_id = update.effective_user.id
    if auth.is_rate_limited(user_id):
        await update.message.reply_text(
            "Too many failed attempts. Account temporarily locked. Try again in 15 minutes."
        )
        return
    token = auth.generate_auth_token(user_id)
    link = f"{AUTH_BASE_URL}/login?token={token}"
    await update.message.reply_text(
        f"Authentication required.\n\n{link}\n\nLink expires in 15 minutes."
    )


async def _send_response(
    bot, chat_id: int, thread_id: int | None, response: str
) -> None:
    """Send a response, splitting at Telegram's message limit.

    Retries each chunk up to SEND_RETRY_ATTEMPTS times with exponential
    backoff before giving up.
    """
    send_kwargs: dict = {"chat_id": chat_id}
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id
    for i in range(0, len(response), TELEGRAM_MSG_LIMIT):
        chunk = response[i : i + TELEGRAM_MSG_LIMIT]
        last_exc: Exception | None = None
        for attempt in range(SEND_RETRY_ATTEMPTS):
            try:
                await bot.send_message(text=chunk, **send_kwargs)
                last_exc = None
                break
            except Exception as e:
                last_exc = e
                delay = SEND_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    "Telegram send failed (attempt %d/%d) for chat=%s thread=%s "
                    "chunk_start=%d: %s — retrying in %.1fs",
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
                "Telegram send failed after %d attempts for chat=%s thread=%s "
                "chunk_start=%d: %s",
                SEND_RETRY_ATTEMPTS,
                chat_id,
                thread_id,
                i,
                last_exc,
            )
            raise last_exc


async def _notify_delivery_failure(
    bot, chat_id: int, thread_id: int | None, label: str
) -> None:
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
        typing_task = asyncio.create_task(
            keep_typing(chat_id, thread_id, stop_typing, bot)
        )

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                _executor, run_claude, text, session_key
            )
        except Exception as e:
            logger.error("Error replaying %s: %s", f.stem, e)
            response = f"Error: {e}"
        finally:
            stop_typing.set()
            await typing_task

        full_response = "[Recovered after bridge restart]\n\n" + response

        send_kwargs = {"chat_id": chat_id}
        if thread_id is not None:
            send_kwargs["message_thread_id"] = thread_id

        for i in range(0, len(full_response), TELEGRAM_MSG_LIMIT):
            await bot.send_message(
                text=full_response[i : i + TELEGRAM_MSG_LIMIT], **send_kwargs
            )

        logger.info("Replayed pending message %s", f.stem)


# ---------------------------------------------------------------------------
# Message and photo handlers
# ---------------------------------------------------------------------------


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _shutting_down:
        await update.message.reply_text(
            "Bridge is shutting down. Message not processed — please resend in a moment."
        )
        return

    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logger.warning("Unauthorized user %d attempted access", user_id)
        return

    if not await _check_auth(update):
        await _send_auth_link(update)
        return

    text = update.message.text
    if not text:
        return

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
        await update.message.reply_text(
            f"Queued ({depth}) — will send when current response finishes."
        )
        logger.info("Queued message for %s (depth: %d)", key, depth)
        _log_activity("message_queued", session_key=key, depth=depth)
        return

    _processing_sessions.add(key)
    _session_start_times[key] = time.time()
    pending_id = save_pending(chat_id, thread_id, text, key)

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        keep_typing(chat_id, thread_id, stop_typing, context.bot)
    )

    try:
        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(_executor, run_claude, text, key)
        except Exception as e:
            logger.error("Error running claude for %s: %s", key, e)
            response = f"Error: {e}"

        # Quota hit — hand off to Forge instead of sending error to user
        if response.startswith(QUOTA_HIT_PREFIX):
            original_msg = response[len(QUOTA_HIT_PREFIX):]
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
                combined = "\n\n---\n\n".join(
                    f"[Follow-up {i + 1}]\n{msg}" for i, msg in enumerate(batch)
                )
            try:
                response = await loop.run_in_executor(
                    _executor, run_claude, combined, key
                )
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
        _session_start_times.pop(key, None)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _shutting_down:
        await update.message.reply_text(
            "Bridge is shutting down. Photo not processed — please resend in a moment."
        )
        return

    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logger.warning("Unauthorized user %d attempted photo access", user_id)
        return

    if not await _check_auth(update):
        await _send_auth_link(update)
        return

    photo = update.message.photo[-1]  # highest resolution
    caption = update.message.caption or "Describe this image."

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    tg_file = await context.bot.get_file(photo.file_id)
    local_path = PHOTO_DIR / f"{photo.file_unique_id}.jpg"
    await tg_file.download_to_drive(local_path)
    logger.info("Downloaded photo to %s for %s", local_path, key)
    _log_activity("photo", user_id=user_id, session_key=key, caption_len=len(caption))

    prompt = (
        f"{caption}\n\n"
        f"[An image has been saved to {local_path} — "
        f"use the Read tool to view it before responding.]"
    )

    pending_id = save_pending(chat_id, thread_id, prompt, key)

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        keep_typing(chat_id, thread_id, stop_typing, context.bot)
    )

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
    finally:
        stop_typing.set()
        await typing_task
        local_path.unlink(missing_ok=True)


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
        "/remote-control - Start claude remote-control in this topic's project dir\n"
        "/remote-control stop - Stop remote-control\n"
        "/kill - Kill active Claude process\n"
        "/restart - Restart the bridge\n"
        "/auth - Authenticate or check auth status\n"
        "/lock - Lock session (/lock all for all sessions)\n"
        "/ping - Check if bridge is alive\n\n"
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
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"setproject:{name}")]
            for name in projects
        ]
        buttons.append(
            [
                InlineKeyboardButton(
                    "Clear (use ~/Developer)", callback_data="setproject:__clear__"
                )
            ]
        )
        await update.message.reply_text(
            "Pick a project (A-Z).\nOr type: /setproject <path>",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    rel_path = args[0]
    abs_path = os.path.join(WORKING_DIR, rel_path)
    if not os.path.isdir(abs_path):
        await update.message.reply_text(f"Directory not found: ~/Developer/{rel_path}")
        return

    set_chat_project(key, rel_path)
    clear_session(key)
    chat_title = update.effective_chat.title or "DM"
    await update.message.reply_text(
        f"Project set: ~/Developer/{rel_path}\n"
        f"Chat: {chat_title}\n"
        f"Session reset. Claude will run from this directory."
    )
    logger.info("Project set to %s for %s (%s)", rel_path, key, chat_title)


async def callback_setproject(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
        await query.edit_message_text(
            "Project cleared. Using default: ~/Developer\nSession reset."
        )
        logger.info("Project cleared for %s", key)
        return

    abs_path = os.path.join(WORKING_DIR, rel_path)
    if not os.path.isdir(abs_path):
        await query.edit_message_text(f"Directory not found: ~/Developer/{rel_path}")
        return

    set_chat_project(key, rel_path)
    clear_session(key)
    chat_title = update.effective_chat.title or "DM"
    await query.edit_message_text(
        f"Project set: ~/Developer/{rel_path}\n"
        f"Chat: {chat_title}\n"
        f"Session reset. Claude will run from this directory."
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


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send an authentication link."""
    import datetime

    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return
    if auth.is_authenticated(user_id):
        info = auth.get_session_info(user_id)
        if info:
            authed_at = datetime.datetime.fromtimestamp(
                info["authenticated_at"], tz=datetime.timezone.utc
            )
            expires_at = authed_at + datetime.timedelta(
                seconds=auth.SESSION_EXPIRY_SECONDS
            )
            await update.message.reply_text(
                f"Already authenticated.\n"
                f"Since: {authed_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                f"Expires: {expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"Use /lock to end your session."
            )
            return
    await _send_auth_link(update)


async def cmd_lock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Lock the current session immediately."""
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    args = context.args
    if args and args[0] == "all":
        count = auth.lock_all_sessions()
        await update.message.reply_text(f"Locked {count} session(s).")
        logger.info("User %d locked all sessions", user_id)
    else:
        auth.lock_session(user_id)
        await update.message.reply_text("Session locked. Use /auth to re-authenticate.")
        logger.info("User %d locked their session", user_id)


async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kill the active Claude process for this chat/topic."""
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    proc = _active_procs.get(key)
    if proc and proc.poll() is None:
        proc.kill()
        await update.message.reply_text(
            "Killed active Claude process. Session preserved — next message resumes."
        )
        logger.info(
            "User %d killed Claude process for %s (pid %d)", user_id, key, proc.pid
        )
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
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    await update.message.reply_text("Restarting bridge...")
    logger.info("User %d triggered bridge restart", user_id)

    for key, proc in list(_active_procs.items()):
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


async def cmd_remote_control(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Start or stop claude remote-control in this topic's project dir."""
    global _remote_proc, _remote_proc_key

    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

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
        logger.info(
            "Replaced existing remote-control process (pid %d)", _remote_proc.pid
        )

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
        await update.message.reply_text(
            f"Remote control exited (code {proc.returncode}):\n{output}"
        )
        _remote_proc = None
        _remote_proc_key = None
    else:
        output = "\n".join(lines) if lines else "(waiting for connection info...)"
        await update.message.reply_text(
            f"Remote control running (pid {proc.pid}):\n{output}\n\nUse /remote stop to shut it down."
        )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _processing_sessions:
        await update.message.reply_text("pong — no active sessions")
        return
    now = time.time()
    lines = ["pong — active sessions:"]
    for key in sorted(_processing_sessions):
        started = _session_start_times.get(key)
        if started:
            elapsed = int(now - started)
            mins, secs = divmod(elapsed, 60)
            lines.append(f"  {key}: running {mins}m{secs:02d}s")
        else:
            lines.append(f"  {key}: running (start time unknown)")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Infrastructure: auth notification, stall detector, lifecycle
# ---------------------------------------------------------------------------


async def _auth_notify(
    event_type: str, telegram_user_id: int, details: str = ""
) -> None:
    """Send auth event alerts to the admin via Telegram."""
    if not ALLOWED_USER_IDS:
        return
    labels = {
        "authenticated": "NEW AUTH",
        "denied": "ACCESS DENIED",
        "ip_changed": "IP CHANGE",
        "locked": "SESSION LOCKED",
        "expired": "SESSION EXPIRED",
    }
    label = labels.get(event_type, event_type.upper())
    msg = f"[{label}] User {telegram_user_id}\n{details}"
    for admin_id in ALLOWED_USER_IDS:
        try:
            await _bot_instance.send_message(chat_id=admin_id, text=msg)
        except Exception:
            logger.debug("Failed to send auth alert to %d", admin_id)


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
        for key, proc in list(_active_procs.items()):
            if proc.poll() is not None:
                _proc_last_active.pop(key, None)
                continue
            cpu = _get_proc_cpu(proc.pid)
            if cpu is None:
                continue
            if cpu >= STALL_CPU_THRESHOLD:
                _proc_last_active[key] = now
                continue
            last_active = _proc_last_active.get(key, now)
            if key not in _proc_last_active:
                _proc_last_active[key] = now
                continue
            stall_duration = now - last_active
            if stall_duration >= STALL_TIMEOUT:
                logger.warning(
                    "Killing stalled Claude process for %s (pid %d, idle %.0fs)",
                    key,
                    proc.pid,
                    stall_duration,
                )
                proc.kill()
                _proc_last_active.pop(key, None)
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
                        send_kwargs: dict = {"chat_id": chat_id}
                        if thread_id is not None:
                            send_kwargs["message_thread_id"] = thread_id
                        await _bot_instance.send_message(
                            text=f"Killed stalled Claude process (idle {stall_duration / 60:.0f} min). Send your message again to retry.",
                            **send_kwargs,
                        )
                    except Exception:
                        logger.debug(
                            "Failed to notify about stalled process for %s", key
                        )


async def post_init(app: Application) -> None:
    """Register bot commands and replay any messages lost during previous crash."""
    global _bot_instance
    _bot_instance = app.bot
    auth.set_notify_callback(_auth_notify)

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
        BotCommand("remote_control", "Start/stop claude remote-control in project dir"),
        BotCommand("kill", "Kill active Claude process"),
        BotCommand("restart", "Restart the bridge"),
        BotCommand("auth", "Authenticate or check auth status"),
        BotCommand("lock", "Lock session (use 'lock all' for all sessions)"),
        BotCommand("ping", "Check if bridge is alive"),
    ]
    await app.bot.set_my_commands(commands)
    logger.info("Bot commands registered with Telegram")

    asyncio.create_task(_stall_detector())
    logger.info(
        "Stall detector started (poll=%ds, timeout=%ds)",
        STALL_POLL_INTERVAL,
        STALL_TIMEOUT,
    )

    await replay_pending(app.bot)

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

    for key, proc in list(_active_procs.items()):
        if proc.poll() is None:
            logger.info("Sending SIGTERM to Claude process for %s (pid %d)", key, proc.pid)
            proc.terminate()

    deadline = time.time() + SHUTDOWN_PROCESS_TIMEOUT
    for key, proc in list(_active_procs.items()):
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
    signal.signal(signal.SIGTERM, _graceful_shutdown)
    signal.signal(signal.SIGINT, _graceful_shutdown)

    app = (
        Application.builder()
        .token(BOT_TOKEN.reveal())
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("clearnew", cmd_clearnew))
    app.add_handler(CommandHandler("setproject", cmd_setproject))
    app.add_handler(CallbackQueryHandler(callback_setproject, pattern=r"^setproject:"))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("kill", cmd_kill))
    app.add_handler(CommandHandler("remote_control", cmd_remote_control))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    logger.info(
        "Bridge started (max_workers=%d). Polling for Telegram messages...",
        MAX_WORKERS,
    )
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
