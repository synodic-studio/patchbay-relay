"""Chat-to-model mapping and per-message model prefix parsing."""

import json
import re

from .config import BASE_DIR

CHAT_MODELS_FILE = BASE_DIR / "chat_models.json"
VALID_MODELS = {"opus", "sonnet", "haiku"}

# Prefix pattern: message starts with !opus, !sonnet, !haiku (or !o, !s, !h)
# followed by whitespace and the actual message.
_MODEL_SHORTCUTS = {"o": "opus", "s": "sonnet", "h": "haiku"}
_PREFIX_RE = re.compile(
    r"^!(" + "|".join(VALID_MODELS | set(_MODEL_SHORTCUTS.keys())) + r")\s+",
    re.IGNORECASE,
)


def _load_chat_models() -> dict[str, str]:
    """Load session_key -> model alias mapping."""
    if CHAT_MODELS_FILE.exists():
        try:
            return json.loads(CHAT_MODELS_FILE.read_text())
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _save_chat_models(models: dict[str, str]) -> None:
    CHAT_MODELS_FILE.write_text(json.dumps(models, indent=2) + "\n")


def get_chat_model(session_key: str) -> str | None:
    """Return the sticky model alias for a chat, or None for default."""
    return _load_chat_models().get(session_key)


def set_chat_model(session_key: str, model: str | None) -> None:
    """Set or clear the sticky model for a chat."""
    models = _load_chat_models()
    if model is None:
        models.pop(session_key, None)
    else:
        models[session_key] = model
    _save_chat_models(models)


def extract_model_prefix(message: str) -> tuple[str | None, str]:
    """Check if message starts with a model prefix like !sonnet or !s.

    Returns (model_name, cleaned_message). If no prefix, returns (None, original).
    """
    m = _PREFIX_RE.match(message)
    if not m:
        return None, message
    tag = m.group(1).lower()
    model = _MODEL_SHORTCUTS.get(tag, tag)
    return model, message[m.end() :]
