"""Chat-to-effort mapping for Claude Code's --effort flag."""

import json

from .config import BASE_DIR

CHAT_EFFORTS_FILE = BASE_DIR / "chat_efforts.json"
VALID_EFFORTS = ["low", "medium", "high", "xhigh", "max"]
DEFAULT_EFFORT = "high"


def _load_chat_efforts() -> dict[str, str]:
    if CHAT_EFFORTS_FILE.exists():
        try:
            return json.loads(CHAT_EFFORTS_FILE.read_text())
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _save_chat_efforts(efforts: dict[str, str]) -> None:
    CHAT_EFFORTS_FILE.write_text(json.dumps(efforts, indent=2) + "\n")


def get_chat_effort(session_key: str) -> str | None:
    """Return the sticky effort for a chat, or None to use DEFAULT_EFFORT."""
    return _load_chat_efforts().get(session_key)


def set_chat_effort(session_key: str, effort: str | None) -> None:
    """Set or clear the sticky effort for a chat."""
    efforts = _load_chat_efforts()
    if effort is None:
        efforts.pop(session_key, None)
    else:
        efforts[session_key] = effort
    _save_chat_efforts(efforts)


def resolve_effort(session_key: str) -> str:
    """Resolve the effort to use: per-chat override, else DEFAULT_EFFORT."""
    return get_chat_effort(session_key) or DEFAULT_EFFORT
