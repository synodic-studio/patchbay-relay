#!/usr/bin/env python3
"""Claude Code Telegram Bridge

Thin relay: Telegram messages -> claude -p -> Telegram responses.
Claude Code is the brain. This script is just a phone line.
"""

import asyncio
import logging
import os
import subprocess
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("bridge")

TELEGRAM_MSG_LIMIT = 4096


def run_claude(message: str) -> str:
    """Invoke claude CLI in one-shot mode and return its output."""
    result = subprocess.run(
        [
            CLAUDE_PATH,
            "-p",
            message,
            "--output-format",
            "text",
            "--dangerously-skip-permissions",
            "--mcp-config",
            PA_MCP_CONFIG,
            "--append-system-prompt",
            (
                "Bryan is messaging you via Telegram from his phone. "
                "Keep responses concise - he's on mobile. "
                "You have full access to all your MCP tools and can do real work."
            ),
        ],
        capture_output=True,
        text=True,
        timeout=CLAUDE_TIMEOUT,
        cwd=WORKING_DIR,
    )
    response = result.stdout.strip()
    if not response and result.stderr:
        return f"(claude produced no output. stderr: {result.stderr[:500]})"
    return response or "(no output)"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id

    if ALLOWED_USER_IDS and user_id not in ALLOWED_USER_IDS:
        logger.warning("Unauthorized user %d attempted access", user_id)
        return

    text = update.message.text
    if not text:
        return

    logger.info("From %d: %s", user_id, text[:80])
    await update.message.chat.send_action("typing")

    try:
        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(None, run_claude, text)
    except subprocess.TimeoutExpired:
        response = f"Claude timed out after {CLAUDE_TIMEOUT}s."
    except Exception as e:
        logger.error("Error running claude: %s", e)
        response = f"Error: {e}"

    # Telegram caps messages at 4096 chars
    for i in range(0, len(response), TELEGRAM_MSG_LIMIT):
        await update.message.reply_text(response[i : i + TELEGRAM_MSG_LIMIT])


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    await update.message.reply_text(
        f"Claude Code bridge active.\nYour Telegram user ID: {uid}\n\n"
        "Add this to ALLOWED_USER_IDS in your .env to lock it down."
    )


async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("pong")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Bridge started. Polling for Telegram messages...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
