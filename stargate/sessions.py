"""Session management: persistence, expiry, sanitization, and pending messages."""

import json
import time
import uuid

from .config import (
    PENDING_DIR,
    SESSION_DIR,
    SESSION_EXPIRY,
    SESSION_KEY_RE,
    atomic_write_text,
    logger,
    quarantine_file,
)


def _sanitize_session_key(key: str) -> str:
    """Sanitize a session key to prevent path traversal.

    Session keys are used in filenames (e.g. sessions/{key}.json).
    Reject any key containing path separators or traversal sequences,
    then validate the format is alphanumeric with underscores/hyphens only.
    """
    if not key:
        raise ValueError("Invalid session key format: empty string")
    if "/" in key or "\\" in key or ".." in key:
        raise ValueError(f"Invalid session key format: {key!r}")
    if not SESSION_KEY_RE.match(key):
        raise ValueError(f"Invalid session key format: {key!r}")
    return key


def _session_key(chat_id: int, thread_id: int | None) -> str:
    """Build a unique session key from chat ID and optional forum topic thread ID."""
    if thread_id is not None:
        return f"{chat_id}_{thread_id}"
    return str(chat_id)


def get_session_id(session_key: str) -> str | None:
    """Load the Claude session ID for a chat. Returns None if expired or missing.

    Corrupt or schema-violating session files are quarantined (moved to
    .quarantine/) rather than silently deleted — the self-healing principle.
    """
    session_key = _sanitize_session_key(session_key)
    session_file = SESSION_DIR / f"{session_key}.json"
    if not session_file.exists():
        return None
    try:
        data = json.loads(session_file.read_text())
        last_active = data["last_active"]
        session_id = data["session_id"]
    except (json.JSONDecodeError, KeyError, TypeError, OSError) as e:
        quarantine_file(session_file, f"corrupt session for {session_key}: {e}")
        return None
    if time.time() - last_active > SESSION_EXPIRY:
        try:
            session_file.unlink()
        except OSError:
            pass
        logger.info("Session expired for %s", session_key)
        return None
    return session_id


def save_session_id(session_key: str, session_id: str) -> None:
    """Persist a Claude session ID for a chat. Atomic: crash-safe."""
    session_key = _sanitize_session_key(session_key)
    atomic_write_text(
        SESSION_DIR / f"{session_key}.json",
        json.dumps({"session_id": session_id, "last_active": time.time()}),
    )


def clear_session(session_key: str) -> None:
    """Remove the stored session for a chat."""
    session_key = _sanitize_session_key(session_key)
    session_file = SESSION_DIR / f"{session_key}.json"
    if session_file.exists():
        session_file.unlink()


def save_pending(chat_id: int, thread_id: int | None, text: str, session_key: str) -> str:
    """Save a message as pending before processing. Returns pending ID. Atomic."""
    pending_id = uuid.uuid4().hex[:12]
    atomic_write_text(
        PENDING_DIR / f"{pending_id}.json",
        json.dumps(
            {
                "chat_id": chat_id,
                "thread_id": thread_id,
                "text": text,
                "session_key": session_key,
                "timestamp": time.time(),
            }
        ),
    )
    return pending_id


def clear_pending(pending_id: str) -> None:
    """Remove a pending message file."""
    (PENDING_DIR / f"{pending_id}.json").unlink(missing_ok=True)


def mark_stall_kill(session_key: str, idle_minutes: float) -> None:
    """Record that a session's prior run was killed by the stall detector.
    Consumed on the next invocation to warn Claude against repeating the
    headless-unsafe command (XCUITest, GUI osascript, etc.) that likely hung it.
    """
    session_key = _sanitize_session_key(session_key)
    atomic_write_text(
        SESSION_DIR / f"{session_key}.stalled",
        json.dumps({"ts": time.time(), "idle_minutes": idle_minutes}),
    )


def consume_stall_kill(session_key: str) -> dict | None:
    """Read and clear the stall-kill marker, if any."""
    session_key = _sanitize_session_key(session_key)
    marker = SESSION_DIR / f"{session_key}.stalled"
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text())
    except (json.JSONDecodeError, KeyError):
        data = None
    marker.unlink(missing_ok=True)
    return data
