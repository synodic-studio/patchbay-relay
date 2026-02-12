#!/usr/bin/env python3
"""Claude Code Telegram Bridge

Thin relay: Telegram messages -> claude -p -> Telegram responses.
Claude Code is the brain. This script is just a phone line.

Conversation continuity: each chat maintains a session ID so consecutive
messages share context. Sessions auto-expire after 2 hours of inactivity.
Use /new to start a fresh session.
"""

import asyncio
import json
import logging
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

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
SESSION_EXPIRY = int(os.environ.get("SESSION_EXPIRY", "7200"))  # 2 hours
MAX_TIMEOUT = int(os.environ.get("MAX_TIMEOUT", "1800"))  # 30 min safety valve

SESSION_DIR = Path(__file__).parent / "sessions"
SESSION_DIR.mkdir(exist_ok=True)

# Single worker = messages processed sequentially, no session conflicts
_executor = ThreadPoolExecutor(max_workers=1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bridge")

TELEGRAM_MSG_LIMIT = 4096
TYPING_INTERVAL = 4  # seconds between typing indicators


def get_session_id(chat_id: int) -> str | None:
    session_file = SESSION_DIR / f"{chat_id}.json"
    if not session_file.exists():
        return None
    try:
        data = json.loads(session_file.read_text())
    except (json.JSONDecodeError, KeyError):
        session_file.unlink()
        return None
    if time.time() - data["last_active"] > SESSION_EXPIRY:
        session_file.unlink()
        logger.info("Session expired for chat %d", chat_id)
        return None
    return data["session_id"]


def save_session_id(chat_id: int, session_id: str) -> None:
    (SESSION_DIR / f"{chat_id}.json").write_text(
        json.dumps({"session_id": session_id, "last_active": time.time()})
    )


def clear_session(chat_id: int) -> None:
    session_file = SESSION_DIR / f"{chat_id}.json"
    if session_file.exists():
        session_file.unlink()


def parse_claude_response(stdout: str, chat_id: int) -> str:
    """Extract text and session_id from claude JSON output."""
    try:
        events = json.loads(stdout)
        result_event = next(
            (e for e in reversed(events) if e.get("type") == "result"), None
        )
        if result_event:
            new_session_id = result_event.get("session_id")
            if new_session_id:
                save_session_id(chat_id, new_session_id)
                logger.info(
                    "Saved session %s for chat %d", new_session_id[:12], chat_id
                )
            return result_event.get("result", "(no result text)")
        for e in reversed(events):
            if e.get("type") == "assistant":
                content = e.get("message", {}).get("content", [])
                texts = [c["text"] for c in content if c.get("type") == "text"]
                if texts:
                    return "\n".join(texts)
        return "(no parseable response)"
    except (json.JSONDecodeError, TypeError):
        return stdout


def run_claude(message: str, chat_id: int) -> str:
    """Invoke claude CLI via Popen. Does not kill on timeout."""
    session_id = get_session_id(chat_id)

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
        "Bryan is messaging you via Telegram from his phone. Keep responses concise - he's on mobile. You have full access to all your MCP tools and can do real work. For email access, use the himalaya CLI: 'himalaya envelope list --account icloud' or '--account gmail' to list emails, 'himalaya message read <id> --account <account>' to read them. Bryan's accounts: iCloud (REDACTED@example.com) and Gmail (REDACTED@example.com). IMPORTANT: NEVER use the AskUserQuestion tool - it requires interactive terminal UI that doesn't work through Telegram. Instead, ask questions as plain text in your response and let Bryan reply naturally.",
    ]

    if session_id:
        cmd.extend(["--resume", session_id])
        logger.info("Resuming session %s", session_id[:12])

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=WORKING_DIR,
    )

    # Wait for completion — no kill. Safety valve at MAX_TIMEOUT.
    try:
        stdout, stderr = proc.communicate(timeout=MAX_TIMEOUT)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return f"Claude hit the {MAX_TIMEOUT // 60} minute safety limit. The work may be partially saved — check git status."

    stdout = stdout.strip()
    if not stdout:
        if stderr:
            return f"(no output. stderr: {stderr[:500]})"
        return "(no output)"

    return parse_claude_response(stdout, chat_id)


async def keep_typing(chat_id: int, stop_event: asyncio.Event, bot) -> None:
    """Send typing indicator every few seconds until stop_event is set."""
    while not stop_event.is_set():
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TYPING_INTERVAL)
            return
        except asyncio.TimeoutError:
            continue


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logger.warning("Unauthorized user %d attempted access", user_id)
        return

    text = update.message.text
    if not text:
        return

    chat_id = update.effective_chat.id
    logger.info("From %d: %s", user_id, text[:80])

    # Start persistent typing indicator
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(
        keep_typing(chat_id, stop_typing, context.bot)
    )

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(_executor, run_claude, text, chat_id)
    except Exception as e:
        logger.error("Error running claude: %s", e)
        response = f"Error: {e}"
    finally:
        stop_typing.set()
        await typing_task

    for i in range(0, len(response), TELEGRAM_MSG_LIMIT):
        await update.message.reply_text(response[i : i + TELEGRAM_MSG_LIMIT])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Claude Code bridge active.\nYour Telegram user ID: {uid}\n\n"
        "Commands:\n"
        "/new - Start a fresh conversation\n"
        "/ping - Check if bridge is alive"
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    clear_session(chat_id)
    await update.message.reply_text("Fresh session started.")
    logger.info("Session cleared for chat %d", chat_id)


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")


async def post_init(app: Application) -> None:
    """Register bot commands so they appear in Telegram's UI menu."""
    from telegram import BotCommand

    await app.bot.set_my_commands(
        [
            BotCommand("new", "Start a fresh conversation"),
            BotCommand("ping", "Check if bridge is alive"),
        ]
    )
    logger.info("Bot commands registered with Telegram")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bridge started. Polling for Telegram messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
