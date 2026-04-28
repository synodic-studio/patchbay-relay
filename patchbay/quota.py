"""Quota/rate-limit detection and Forge handoff."""

import datetime
import re

from .config import FORGE_QUEUE_DIR, logger
from .activity import log_activity
from .parser import _extract_text_from_events

# Patterns confirmed from Claude CLI source:
#   stderr: "Please wait and try again later"
#   API error type: "rate_limit_error" (429), "overloaded_error" (529)
_QUOTA_PATTERNS_STDERR = [
    "please wait and try again later",
    "rate limit",
    "rate_limit",
]
_QUOTA_PATTERNS_ERROR = [
    "rate_limit_error",
    "overloaded_error",
    "rate limit",
    "rate limited",
    "too many requests",
    "usage limit",
]


def is_quota_error(events: list[dict], stderr: str) -> bool:
    """Detect whether a Claude invocation failed due to quota or rate limiting."""
    haystack = stderr.lower()
    if any(p in haystack for p in _QUOTA_PATTERNS_STDERR):
        return True
    result = next((e for e in reversed(events) if e.get("type") == "result"), None)
    if result:
        error = str(result.get("error", "")).lower()
        if any(p in error for p in _QUOTA_PATTERNS_ERROR):
            return True
    text = _extract_text_from_events(events)
    if text and len(text) < 300:
        text_lower = text.lower()
        if any(p in text_lower for p in _QUOTA_PATTERNS_ERROR):
            return True
    return False


def handoff_to_forge(
    session_key: str,
    message: str,
    chat_id: int,
    thread_id: int | None,
    session_id: str | None,
    working_dir: str,
) -> bool:
    """Write a Forge queue file so the task can be resumed later.

    Returns True if the queue file was written successfully.
    """
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    today = datetime.date.today().isoformat()

    # Find longest run of backticks to create a safe fence
    backtick_runs = re.findall(r"`+", message)
    max_backticks = max((len(r) for r in backtick_runs), default=0)
    fence = "`" * max(3, max_backticks + 1)

    lines = [
        "# Bridge Quota Recovery",
        f"Updated: {today}",
        "Priority: critical",
        "",
        "## Tasks (ordered)",
        f"1. (no bead) Resume interrupted Telegram session {session_key}",
        "",
        "## Notes",
        "This task was created automatically by the Telegram bridge because a quota",
        "or rate limit was hit mid-conversation. The user's request may be partially",
        "complete — there could be in-progress work (uncommitted code, half-written",
        "responses, open tool calls). Pick up where the previous session left off.",
        "",
        "**Do not start from scratch.** Check the repo for uncommitted changes, open",
        "branches, and any context from the session before continuing.",
        "",
        f"- **Quota hit at:** {now}",
        f"- **Session key:** {session_key}",
        f"- **Session ID:** {session_id or 'none (fresh session)'}",
        f"- **Working directory:** {working_dir}",
        f"- **Chat ID:** {chat_id}",
        f"- **Thread ID:** {thread_id}",
        "",
        "**Original message from the user:**",
        fence,
        message,
        fence,
        "",
        "**Response routing:** When done, send the response back to Telegram.",
        "Use the bot API:",
        "```bash",
        "BOT_TOKEN=$(grep TELEGRAM_BOT_TOKEN ~/Developer/patchbay-relay/.env | cut -d= -f2-)",
        'curl -s -X POST "https://api.telegram.org/bot$BOT_TOKEN/sendMessage" \\',
        f"  -d chat_id={chat_id} \\",
        f"  -d message_thread_id={thread_id} \\",
        '  -d text="YOUR_RESPONSE_HERE"',
        "```",
    ]

    queue_file = FORGE_QUEUE_DIR / f"bridge-recovery-{session_key.replace('-', '')[:20]}.md"
    try:
        FORGE_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        queue_file.write_text("\n".join(lines) + "\n")
        logger.info("Wrote Forge queue file: %s", queue_file.name)
        log_activity(
            "forge_handoff",
            session_key=session_key,
            queue_file=str(queue_file.name),
            chat_id=chat_id,
            thread_id=thread_id,
        )
        return True
    except Exception as e:
        logger.error("Failed to write Forge queue file: %s", e)
        return False
