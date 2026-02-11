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
CLAUDE_TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "300"))
PA_MCP_CONFIG = os.environ.get(
    "PA_MCP_CONFIG",
    os.path.expanduser("~/Developer/claude-pa/.mcp.json"),
)
SESSION_EXPIRY = int(os.environ.get("SESSION_EXPIRY", "7200"))  # 2 hours

SESSION_DIR = Path(__file__).parent / "sessions"
SESSION_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bridge")

TELEGRAM_MSG_LIMIT = 4096


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


def run_claude(message: str, chat_id: int) -> str:
    """Invoke claude CLI and return its output. Resumes session if one exists."""
    session_id = get_session_id(chat_id)

    cmd = [
        CLAUDE_PATH,
        "-p",
        message,
        "--output-format",
        "json",
        "--dangerously-skip-permissions",
        "--mcp-config",
        PA_MCP_CONFIG,
        "--append-system-prompt",
        (
            "Bryan is messaging you via Telegram from his phone. "
            "Keep responses concise - he's on mobile. "
            "You have full access to all your MCP tools and can do real work."
        ),
    ]

    if session_id:
        cmd.extend(["--resume", session_id])
        logger.info("Resuming session %s", session_id[:12])

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        cwd=WORKING_DIR,
    )

    stdout = result.stdout.strip()
    if not stdout:
        if result.stderr:
            return f"(no output. stderr: {result.stderr[:500]})"
        return "(no output)"

    # Parse JSON response: output is a list of events.
    # Last element (type "result") has the text and session_id.
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
        # No result event found — return raw text of last assistant message
        for e in reversed(events):
            if e.get("type") == "assistant":
                content = e.get("message", {}).get("content", [])
                texts = [c["text"] for c in content if c.get("type") == "text"]
                if texts:
                    return "\n".join(texts)
        return "(no parseable response)"
    except (json.JSONDecodeError, TypeError):
        return stdout


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
    await update.message.chat.send_action("typing")

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, run_claude, text, chat_id)
    except subprocess.TimeoutExpired:
        response = f"Claude timed out after {CLAUDE_TIMEOUT}s."
    except Exception as e:
        logger.error("Error running claude: %s", e)
        response = f"Error: {e}"

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


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bridge started. Polling for Telegram messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
