"""Outbound message log for heartbeat notifications and response audit.

Two distinct log sources share one per-session file:

1. Agent notifications (source != "claude-response"): agents send
   messages to Telegram via Bot API, bypassing Patchbay. log_outbound()
   records these so a subsequent Claude session can see what was sent.
2. Claude response audit (source == "claude-response"):
   log_outbound_response() records what Patchbay itself sent in reply to
   a Telegram message, for diagnosing client-side render drops.

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

# Cap for agent notifications (per thread). The system prompt surfaces
# the most recent of these to the next Claude session.
MAX_ENTRIES = 10

# Cap for claude-response audit chunks (per thread). Larger than
# MAX_ENTRIES because a single logical response may be several chunks,
# and the goal is to retain diagnostic history for a handful of recent
# responses, not just the most recent single send.
MAX_RESPONSE_ENTRIES = 50

_RESPONSE_SOURCE = "claude-response"


def _outbound_file(session_key: str) -> Path:
    return OUTBOUND_DIR / f"{session_key}.jsonl"


def _outbound_lock_file(session_key: str) -> Path:
    return OUTBOUND_DIR / f".{session_key}.lock"


def _prune_entries(lines: list[str]) -> list[str]:
    """Cap agent-notification and claude-response entries independently.

    Preserves original order within each category. Unparseable lines are
    dropped silently (they'd fail the reader anyway).
    """
    notifications: list[str] = []
    responses: list[str] = []
    for line in lines:
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("source") == _RESPONSE_SOURCE:
            responses.append(line)
        else:
            notifications.append(line)
    notifications = notifications[-MAX_ENTRIES:]
    responses = responses[-MAX_RESPONSE_ENTRIES:]
    # Re-sort by timestamp so downstream readers see chronological order.
    combined = notifications + responses
    combined.sort(key=lambda line: json.loads(line).get("ts", 0))
    return combined


def _append_with_lock(session_key: str, entry: str, failure_msg: str) -> None:
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
        existing = _prune_entries(existing)
        atomic_write_text(path, "\n".join(existing) + "\n")
    except OSError as e:
        logger.warning("%s %s: %s", failure_msg, session_key, e)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


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
    _append_with_lock(session_key, entry, "Failed to log outbound for")


def log_outbound_response(
    session_key: str,
    chunk_index: int,
    chunk_total: int,
    raw: str,
    md: str | None,
    parse_mode: str,
    status: str,
) -> None:
    """Append an outbound Claude-response chunk to the audit log.

    Distinct from log_outbound (agent notifications): records what
    Patchbay itself sent in reply to a Telegram message, so client-side
    render drops can be diagnosed.

    Args:
        session_key: Telegram session key ({chat_id}_{thread_id})
        chunk_index: 0-based index of this chunk within the logical response
        chunk_total: total number of chunks in this response
        raw: the plain markdown text (what Claude emitted for this chunk)
        md: the MarkdownV2-converted text actually sent, or None if plain
        parse_mode: "MarkdownV2" or "plain"
        status: "ok" on successful send, or the exception class name on failure
    """
    entry = json.dumps(
        {
            "ts": time.time(),
            "source": _RESPONSE_SOURCE,
            "session_key": session_key,
            "chunk_index": chunk_index,
            "chunk_total": chunk_total,
            "raw_text": raw,
            "md_text": md,
            "parse_mode": parse_mode,
            "http_status": status,
        }
    )
    _append_with_lock(session_key, entry, "Failed to log outbound response for")


def get_recent_outbound(session_key: str, max_age: float = 86400.0) -> list[dict]:
    """Read recent agent notifications for a session key.

    Filters out claude-response audit entries — those are only for
    diagnostic use and would both pollute the Claude context and blow up
    the existing consumer (which reads entry["text"]).

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
            except (json.JSONDecodeError, TypeError):
                continue
            if entry.get("source") == _RESPONSE_SOURCE:
                continue
            if entry.get("ts", 0) >= cutoff:
                results.append(entry)
    except OSError:
        return []

    return results
