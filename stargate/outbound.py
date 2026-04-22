"""Outbound message log for heartbeat notifications.

Agents send notifications directly to Telegram via Bot API, bypassing
Stargate. When the user replies, the Claude session has no context about
what was sent. This module bridges that gap by:

1. Providing a log_outbound() function agents call alongside their send.
2. Providing get_recent_outbound() for bridge.py to prepend context.

Log format: one JSON object per line in outbound/{session_key}.jsonl.
"""

import fcntl
import json
import os
import time
from pathlib import Path

from .config import BASE_DIR, atomic_write_text, logger

OUTBOUND_DIR = BASE_DIR / "outbound"
OUTBOUND_DIR.mkdir(exist_ok=True)

# Keep last N messages per thread (prune on write)
MAX_ENTRIES = 10


def _outbound_file(session_key: str) -> Path:
    return OUTBOUND_DIR / f"{session_key}.jsonl"


def _outbound_lock_file(session_key: str) -> Path:
    return OUTBOUND_DIR / f".{session_key}.lock"


def log_outbound(session_key: str, text: str, source: str) -> None:
    """Append an outbound notification to the log for a session key.

    Args:
        session_key: Telegram session key ({chat_id}_{thread_id})
        text: The message text that was sent to Telegram
        source: Agent name that sent it (e.g. "buddy", "feathers")

    Locked + atomic: concurrent writers cannot lose each other's entries.
    """
    entry = json.dumps(
        {
            "ts": time.time(),
            "source": source,
            "text": text,
        }
    )

    path = _outbound_file(session_key)
    lock_path = _outbound_lock_file(session_key)

    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        logger.warning("Failed to open outbound lock %s: %s", lock_path, e)
        return

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        existing: list[str] = []
        if path.exists():
            try:
                existing = [line for line in path.read_text().splitlines() if line]
            except OSError as e:
                logger.warning("Failed to read outbound %s: %s", path, e)
        existing.append(entry)
        if len(existing) > MAX_ENTRIES:
            existing = existing[-MAX_ENTRIES:]
        atomic_write_text(path, "\n".join(existing) + "\n")
    except OSError as e:
        logger.warning("Failed to log outbound for %s: %s", session_key, e)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def get_recent_outbound(session_key: str, max_age: float = 86400.0) -> list[dict]:
    """Read recent outbound messages for a session key.

    Args:
        session_key: Telegram session key
        max_age: Only return messages newer than this many seconds (default 24h)

    Returns:
        List of dicts with keys: ts, source, text (newest last)
    """
    path = _outbound_file(session_key)
    if not path.exists():
        return []

    cutoff = time.time() - max_age
    results = []
    try:
        for line in path.read_text().strip().splitlines():
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("ts", 0) >= cutoff:
                    results.append(entry)
            except (json.JSONDecodeError, TypeError):
                continue
    except OSError:
        return []

    return results
