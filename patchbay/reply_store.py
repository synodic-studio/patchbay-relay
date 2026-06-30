"""Sent-message text store for reply-context injection.

When the bot sends a message to Telegram, record (chat_id, message_id) → text
so inbound replies to bot messages can carry the quoted text into the prompt.

Telegram's Bot API does not echo bot-message text in reply_to_message for
messages sent by the bot itself. This store fills that gap.

Capped at MAX_ENTRIES entries (oldest trimmed first). Each text entry is
capped at MAX_TEXT_LEN characters. Atomic writes for crash safety.
"""

from __future__ import annotations

import json
import time

from .config import REPLY_STORE_FILE, atomic_write_text, logger

MAX_ENTRIES = 1000
MAX_TEXT_LEN = 2000


def _key(chat_id: int, message_id: int) -> str:
    return f"{chat_id}:{message_id}"


def _load() -> dict:
    if not REPLY_STORE_FILE.exists():
        return {}
    try:
        data = json.loads(REPLY_STORE_FILE.read_text())
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def record(chat_id: int, message_id: int, text: str) -> None:
    """Store sent message text keyed by (chat_id, message_id)."""
    if not text or not text.strip():
        return
    store = _load()
    store[_key(chat_id, message_id)] = {
        "t": int(time.time()),
        "text": text[:MAX_TEXT_LEN],
    }
    if len(store) > MAX_ENTRIES:
        sorted_keys = sorted(store.keys(), key=lambda k: store[k].get("t", 0))
        for old_key in sorted_keys[: len(store) - MAX_ENTRIES]:
            del store[old_key]
    try:
        atomic_write_text(REPLY_STORE_FILE, json.dumps(store) + "\n")
    except OSError as exc:
        logger.warning("reply_store write failed: %s", exc)


def lookup(chat_id: int, message_id: int) -> str | None:
    """Return text of a previously sent bot message, or None if not stored."""
    store = _load()
    entry = store.get(_key(chat_id, message_id))
    if isinstance(entry, dict):
        return entry.get("text")
    return None
