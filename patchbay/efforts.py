"""Chat-to-effort mapping for Claude Code's --effort flag."""

import fcntl
import json
import os
from contextlib import contextmanager

from .config import BASE_DIR, atomic_write_text, quarantine_file

CHAT_EFFORTS_FILE = BASE_DIR / "chat_efforts.json"
_EFFORTS_LOCK_FILE = BASE_DIR / ".chat_efforts.lock"
VALID_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = "high"


@contextmanager
def _efforts_lock():
    """Exclusive lock for read-modify-write cycles on chat_efforts.json."""
    fd = os.open(_EFFORTS_LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _load_chat_efforts() -> dict[str, str]:
    if not CHAT_EFFORTS_FILE.exists():
        return {}
    try:
        data = json.loads(CHAT_EFFORTS_FILE.read_text())
        if isinstance(data, dict):
            return data
        quarantine_file(CHAT_EFFORTS_FILE, "not a dict")
        return {}
    except (json.JSONDecodeError, TypeError, OSError) as e:
        quarantine_file(CHAT_EFFORTS_FILE, f"corrupt: {e}")
        return {}


def _save_chat_efforts(efforts: dict[str, str]) -> None:
    atomic_write_text(CHAT_EFFORTS_FILE, json.dumps(efforts, indent=2) + "\n")


def get_chat_effort(session_key: str) -> str | None:
    """Return the sticky effort for a chat, or None to use DEFAULT_EFFORT."""
    return _load_chat_efforts().get(session_key)


def set_chat_effort(session_key: str, effort: str | None) -> None:
    """Set or clear the sticky effort for a chat (locked)."""
    with _efforts_lock():
        efforts = _load_chat_efforts()
        if effort is None:
            efforts.pop(session_key, None)
        else:
            efforts[session_key] = effort
        _save_chat_efforts(efforts)


def resolve_effort(session_key: str) -> str:
    """Resolve the effort to use: per-chat override, else DEFAULT_EFFORT."""
    return get_chat_effort(session_key) or DEFAULT_EFFORT
