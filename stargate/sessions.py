"""Session management: persistence, expiry, sanitization, and pending messages."""

import json
import time
import uuid

from .config import PENDING_DIR, SESSION_DIR, SESSION_EXPIRY, SESSION_KEY_RE, logger


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
    """Load the Claude session ID for a chat. Returns None if expired or missing."""
    session_key = _sanitize_session_key(session_key)
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
    """Persist a Claude session ID for a chat."""
    session_key = _sanitize_session_key(session_key)
    (SESSION_DIR / f"{session_key}.json").write_text(
        json.dumps({"session_id": session_id, "last_active": time.time()})
    )


def clear_session(session_key: str) -> None:
    """Remove the stored session for a chat."""
    session_key = _sanitize_session_key(session_key)
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
    """Remove a pending message file."""
    (PENDING_DIR / f"{pending_id}.json").unlink(missing_ok=True)
