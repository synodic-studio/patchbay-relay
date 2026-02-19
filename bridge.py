#!/usr/bin/env python3
"""Claude Code Telegram Bridge

Thin relay: Telegram messages -> claude -p -> Telegram responses.
Claude Code is the brain. This script is just a phone line.

Conversation continuity: each forum topic (or DM chat) maintains its own
session ID so consecutive messages share context. Sessions auto-expire
after 3 days of inactivity. Use /new to start a fresh session in the
current topic.

Forum topics: Enable "Topics" in your Telegram group settings. Each topic
becomes an independent Claude session, running in parallel.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import auth

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER_IDS: set[int] = set()
_raw = os.environ.get("ALLOWED_USER_IDS", "")
if _raw.strip():
    ALLOWED_USER_IDS = {int(x.strip()) for x in _raw.split(",") if x.strip()}

CLAUDE_PATH = os.environ.get("CLAUDE_PATH", "/opt/homebrew/bin/claude")
WORKING_DIR = os.environ.get("CLAUDE_WORKING_DIR", os.path.expanduser("~/Developer"))
PA_PLUGIN_DIR = os.environ.get(
    "PA_PLUGIN_DIR",
    os.path.expanduser("~/Developer/claude-pa"),
)
SESSION_EXPIRY = int(os.environ.get("SESSION_EXPIRY", "259200"))  # 3 days
MAX_TIMEOUT = int(os.environ.get("MAX_TIMEOUT", "1800"))  # 30 min safety valve
MAX_WORKERS = int(os.environ.get("MAX_WORKERS", "4"))
AUTH_BASE_URL = os.environ.get("AUTH_BASE_URL", "https://auth.kj6.dev")
AUTH_REQUIRED = os.environ.get("AUTH_REQUIRED", "false").lower() == "true"

SESSION_DIR = Path(__file__).parent / "sessions"
SESSION_DIR.mkdir(exist_ok=True)
PENDING_DIR = Path(__file__).parent / "pending"
PENDING_DIR.mkdir(exist_ok=True)
CHAT_PROJECTS_FILE = Path(__file__).parent / "chat_projects.json"


def _load_chat_projects() -> dict[str, str]:
    """Load session_key -> relative project path mapping."""
    if CHAT_PROJECTS_FILE.exists():
        try:
            return json.loads(CHAT_PROJECTS_FILE.read_text())
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _save_chat_projects(projects: dict[str, str]) -> None:
    CHAT_PROJECTS_FILE.write_text(json.dumps(projects, indent=2) + "\n")


def get_chat_working_dir(session_key: str) -> str:
    """Resolve working directory for a chat. Returns absolute path."""
    projects = _load_chat_projects()
    rel_path = projects.get(session_key)
    if rel_path:
        return os.path.join(WORKING_DIR, rel_path)
    return WORKING_DIR


def set_chat_project(session_key: str, rel_path: str | None) -> None:
    """Set or clear the project directory for a chat."""
    projects = _load_chat_projects()
    if rel_path is None:
        projects.pop(session_key, None)
    else:
        projects[session_key] = rel_path
    _save_chat_projects(projects)

_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

# Track active Claude subprocesses per session key so /kill can terminate them
_active_procs: dict[str, subprocess.Popen] = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bridge")

TELEGRAM_MSG_LIMIT = 4096
TYPING_INTERVAL = 4  # seconds between typing indicators
PHOTO_DIR = Path(tempfile.gettempdir()) / "claude-telegram-photos"
PHOTO_DIR.mkdir(exist_ok=True)


def _session_key(chat_id: int, thread_id: int | None) -> str:
    """Build a unique session key from chat ID and optional forum topic thread ID."""
    if thread_id is not None:
        return f"{chat_id}_{thread_id}"
    return str(chat_id)


def get_session_id(session_key: str) -> str | None:
    session_file = SESSION_DIR / f"{session_key}.json"
    if not session_file.exists():
        return None
    try:
        data = json.loads(session_file.read_text())
    except (json.JSONDecodeError, KeyError):
        session_file.unlink()
        return None
    if time.time() - data["last_active"] > SESSION_EXPIRY:
        session_file.unlink()
        logger.info("Session expired for %s", session_key)
        return None
    return data["session_id"]


def save_session_id(session_key: str, session_id: str) -> None:
    (SESSION_DIR / f"{session_key}.json").write_text(
        json.dumps({"session_id": session_id, "last_active": time.time()})
    )


def clear_session(session_key: str) -> None:
    session_file = SESSION_DIR / f"{session_key}.json"
    if session_file.exists():
        session_file.unlink()


def save_pending(
    chat_id: int, thread_id: int | None, text: str, session_key: str
) -> str:
    """Save a message as pending before processing. Returns pending ID."""
    pending_id = uuid.uuid4().hex[:12]
    (PENDING_DIR / f"{pending_id}.json").write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "text": text,
                "session_key": session_key,
                "timestamp": time.time(),
            }
        )
    )
    return pending_id


def clear_pending(pending_id: str) -> None:
    (PENDING_DIR / f"{pending_id}.json").unlink(missing_ok=True)


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
        # If run_claude kills the bridge (e.g. "restart gateway"), the file
        # won't survive to be replayed on the next restart.
        f.unlink()

        loop = asyncio.get_running_loop()
        try:
            response = await loop.run_in_executor(
                _executor, run_claude, text, session_key
            )
        except Exception as e:
            logger.error("Error replaying %s: %s", f.stem, e)
            response = f"Error: {e}"

        full_response = "[Recovered after bridge restart]\n\n" + response

        send_kwargs = {"chat_id": chat_id}
        if thread_id is not None:
            send_kwargs["message_thread_id"] = thread_id

        for i in range(0, len(full_response), TELEGRAM_MSG_LIMIT):
            await bot.send_message(
                text=full_response[i : i + TELEGRAM_MSG_LIMIT], **send_kwargs
            )

        logger.info("Replayed pending message %s", f.stem)


def _format_token_count(n: int) -> str:
    """Format token count: 1234 -> '1.2K', 56 -> '56'."""
    if n >= 1000:
        return f"{n / 1000:.1f}K"
    return str(n)


def _build_usage_footer(result_event: dict) -> str:
    """Build a compact usage footer from the result event metadata."""
    usage = result_event.get("usage", {})
    input_tokens = usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
    output_tokens = usage.get("output_tokens", 0)
    if not input_tokens and not output_tokens:
        return ""
    return f"\n\n[{_format_token_count(input_tokens)} in, {_format_token_count(output_tokens)} out]"


def parse_claude_response(stdout: str, session_key: str) -> str:
    """Extract text and session_id from claude JSON output."""
    try:
        events = json.loads(stdout)
        result_event = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        if result_event:
            new_session_id = result_event.get("session_id")
            if new_session_id:
                save_session_id(session_key, new_session_id)
                logger.info(
                    "Saved session %s for %s", new_session_id[:12], session_key
                )
            text = result_event.get("result", "(no result text)")
            return text + _build_usage_footer(result_event)
        for e in reversed(events):
            if e.get("type") == "assistant":
                content = e.get("message", {}).get("content", [])
                texts = [c["text"] for c in content if c.get("type") == "text"]
                if texts:
                    return "\n".join(texts)
        return "(no parseable response)"
    except (json.JSONDecodeError, TypeError):
        return stdout


def run_claude(message: str, session_key: str) -> str:
    """Invoke claude CLI via Popen. Does not kill on timeout."""
    session_id = get_session_id(session_key)
    chat_cwd = get_chat_working_dir(session_key)

    projects = _load_chat_projects()
    rel_path = projects.get(session_key)
    project_info = f"~/Developer/{rel_path}" if rel_path else "~/Developer (default)"

    system_prompt = (
        "Bryan is messaging you via Telegram from his phone. Keep responses concise - he's on mobile. "
        "You have full access to all your MCP tools and can do real work. "
        "For email access, use the himalaya CLI: 'himalaya envelope list --account icloud' or '--account gmail' to list emails, "
        "'himalaya message read <id> --account <account>' to read them. "
        "Bryan's accounts: iCloud (REDACTED@example.com) and Gmail (REDACTED@example.com). "
        "IMPORTANT: NEVER use the AskUserQuestion tool - it requires interactive terminal UI that doesn't work through Telegram. "
        "Instead, ask questions as plain text in your response and let Bryan reply naturally.\n\n"
        "FORMATTING: Telegram renders messages as plain text — no markdown. "
        "For any tabular or structured data, use ASCII art (aligned columns, dashes, box-drawing characters).\n\n"
        f"Telegram session key: {session_key}\n"
        f"Working directory: {project_info}\n"
        f"Chat projects config: {CHAT_PROJECTS_FILE}\n"
        "You can change your own project directory by editing chat_projects.json "
        "(map session key to a path relative to ~/Developer). "
        "After changing it, tell Bryan to run /new to pick up the new cwd."
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
        "--plugin-dir",
        PA_PLUGIN_DIR,
        "--append-system-prompt",
        system_prompt,
    ]

    if session_id:
        cmd.extend(["--resume", session_id])
        logger.info("Resuming session %s for %s", session_id[:12], session_key)

    logger.info("Launching claude in %s for %s", chat_cwd, session_key)

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=chat_cwd,
    )
    _active_procs[session_key] = proc

    try:
        stdout, stderr = proc.communicate(timeout=MAX_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return f"Claude hit the {MAX_TIMEOUT // 60} minute safety limit. The work may be partially saved — check git status."
    finally:
        _active_procs.pop(session_key, None)

    stdout = stdout.strip()
    if not stdout:
        # Stale session: Claude couldn't find the conversation. Clear and retry.
        if stderr and "No conversation found" in stderr and session_id:
            logger.warning("Stale session %s for %s, retrying fresh", session_id[:12], session_key)
            clear_session(session_key)
            return run_claude(message, session_key)
        if stderr:
            return f"(no output. stderr: {stderr[:500]})"
        return "(no output)"

    return parse_claude_response(stdout, session_key)


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
    return True


async def _send_auth_link(update: Update) -> None:
    """Generate and send an auth link to the user."""
    user_id = update.effective_user.id
    token = auth.generate_auth_token(user_id)
    link = f"{AUTH_BASE_URL}/login?token={token}"
    await update.message.reply_text(
        f"Authentication required.\n\n{link}\n\nLink expires in 15 minutes."
    )


async def cmd_auth(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send an authentication link."""
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return
    if auth.is_authenticated(user_id):
        info = auth.get_session_info(user_id)
        if info:
            import datetime

            authed_at = datetime.datetime.fromtimestamp(
                info["authenticated_at"], tz=datetime.timezone.utc
            )
            expires_at = authed_at + datetime.timedelta(seconds=auth.SESSION_EXPIRY_SECONDS)
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


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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

    pending_id = save_pending(chat_id, thread_id, text, key)

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        keep_typing(chat_id, thread_id, stop_typing, context.bot)
    )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(_executor, run_claude, text, key)
    except Exception as e:
        logger.error("Error running claude for %s: %s", key, e)
        response = f"Error: {e}"
    finally:
        stop_typing.set()
        await typing_task

    for i in range(0, len(response), TELEGRAM_MSG_LIMIT):
        await update.message.reply_text(response[i : i + TELEGRAM_MSG_LIMIT])

    clear_pending(pending_id)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
        response = await loop.run_in_executor(_executor, run_claude, prompt, key)
    except Exception as e:
        logger.error("Error running claude for photo %s: %s", key, e)
        response = f"Error: {e}"
    finally:
        stop_typing.set()
        await typing_task
        local_path.unlink(missing_ok=True)

    for i in range(0, len(response), TELEGRAM_MSG_LIMIT):
        await update.message.reply_text(response[i : i + TELEGRAM_MSG_LIMIT])

    clear_pending(pending_id)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Claude Code bridge active.\nYour Telegram user ID: {uid}\n\n"
        "Commands:\n"
        "/new - Start a fresh conversation (in current topic)\n"
        "/setproject <path> - Set project dir (relative to ~/Developer)\n"
        "/setproject - Clear project binding (use default)\n"
        "/project - Show current project dir\n"
        "/kill - Kill active Claude process\n"
        "/restart - Restart the bridge\n"
        "/auth - Authenticate or check auth status\n"
        "/lock - Lock session (/lock all for all sessions)\n"
        "/ping - Check if bridge is alive\n\n"
        "Each forum topic runs as an independent Claude session."
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)
    clear_session(key)
    await update.message.reply_text("Fresh session started.")
    logger.info("Session cleared for %s", key)


def _get_all_projects() -> list[str]:
    """Return all project directory names sorted alphabetically."""
    dev_path = Path(WORKING_DIR)
    dirs = [
        d.name
        for d in dev_path.iterdir()
        if d.is_dir() and not d.name.startswith((".", "_"))
    ]
    dirs.sort(key=str.lower)
    return dirs


async def cmd_setproject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    thread_id = update.message.message_thread_id
    key = _session_key(chat_id, thread_id)

    args = context.args
    if not args:
        # No args: show project picker buttons
        projects = _get_all_projects()
        buttons = [
            [InlineKeyboardButton(name, callback_data=f"setproject:{name}")]
            for name in projects
        ]
        buttons.append(
            [InlineKeyboardButton("Clear (use ~/Developer)", callback_data="setproject:__clear__")]
        )
        await update.message.reply_text(
            "Pick a project (A-Z).\n"
            "Or type: /setproject <path>",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return

    rel_path = args[0]
    abs_path = os.path.join(WORKING_DIR, rel_path)
    if not os.path.isdir(abs_path):
        await update.message.reply_text(
            f"Directory not found: ~/Developer/{rel_path}"
        )
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
    projects = _load_chat_projects()
    rel_path = projects.get(key)
    if rel_path:
        await update.message.reply_text(f"Project: ~/Developer/{rel_path}")
    else:
        await update.message.reply_text("No project set. Using default: ~/Developer")


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
        proc.terminate()
        await update.message.reply_text("Killed active Claude process. Session preserved — next message resumes.")
        logger.info("User %d killed Claude process for %s (pid %d)", user_id, key, proc.pid)
    else:
        await update.message.reply_text("No active Claude process in this chat.")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Restart the bridge process. Launchd will respawn it."""
    user_id = update.effective_user.id
    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        return

    await update.message.reply_text("Restarting bridge...")
    logger.info("User %d triggered bridge restart", user_id)

    # Terminate all active Claude processes first
    for key, proc in list(_active_procs.items()):
        if proc.poll() is None:
            proc.terminate()
            logger.info("Terminated Claude process for %s (pid %d)", key, proc.pid)

    # Exit non-zero so launchd respawns us
    os.kill(os.getpid(), signal.SIGTERM)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")


async def _auth_notify(event_type: str, telegram_user_id: int, details: str = "") -> None:
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


_bot_instance = None


async def post_init(app: Application) -> None:
    """Register bot commands and replay any messages lost during previous crash."""
    global _bot_instance
    _bot_instance = app.bot
    auth.set_notify_callback(_auth_notify)

    from telegram import BotCommand

    await app.bot.set_my_commands(
        [
            BotCommand("new", "Start a fresh conversation"),
            BotCommand("setproject", "Set project dir (relative to ~/Developer)"),
            BotCommand("project", "Show current project dir"),
            BotCommand("auth", "Authenticate or check auth status"),
            BotCommand("lock", "Lock session (use 'lock all' for all sessions)"),
            BotCommand("kill", "Kill active Claude process"),
            BotCommand("restart", "Restart the bridge"),
            BotCommand("ping", "Check if bridge is alive"),
        ]
    )
    logger.info("Bot commands registered with Telegram")

    await replay_pending(app.bot)


def main() -> None:
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("setproject", cmd_setproject))
    app.add_handler(CallbackQueryHandler(callback_setproject, pattern=r"^setproject:"))
    app.add_handler(CommandHandler("project", cmd_project))
    app.add_handler(CommandHandler("auth", cmd_auth))
    app.add_handler(CommandHandler("lock", cmd_lock))
    app.add_handler(CommandHandler("kill", cmd_kill))
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
