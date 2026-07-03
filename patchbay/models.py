"""Chat-to-model mapping and per-message model prefix parsing.

Pi uses litellm under the hood and supports model IDs in `provider/id` format
(e.g. `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`) as well as
built-in aliases like `small`, `medium`, `large`, `gpt`, `opus`, `write`, etc.

The default model is `small` — a fast, capable litellm alias.
"""

import json
import re
import subprocess
from typing import Tuple

from .config import BASE_DIR

CHAT_MODELS_FILE = BASE_DIR / "chat_models.json"

# Pi's built-in litellm model aliases. These are the short names pi resolves
# through its litellm backend. `small` is the default — fast and capable.
_PI_LITELLM_ALIASES = (
    "small",
    "medium",
    "large",
    "gpt",
    "opus",
    "write",
    "dsf",
    "glm",
)
# Also accept shortcut single chars: s, m, l, g, o, w
_PI_SHORTCUTS: dict[str, str] = {
    "s": "small",
    "m": "medium",
    "l": "large",
    "g": "gpt",
    "o": "opus",
    "w": "write",
}
# Everything we accept via /\model or prefix
VALID_MODELS = _PI_LITELLM_ALIASES + tuple(_PI_SHORTCUTS.keys())
DEFAULT_MODEL = "small"

# Prefix pattern: message starts with !model_name followed by whitespace
_PREFIX_RE = re.compile(
    r"^!(" + "|".join(re.escape(m) for m in VALID_MODELS) + r")\s+",
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


def resolve_model(session_key: str) -> str:
    """Resolve the model to use: per-chat override, else DEFAULT_MODEL."""
    return get_chat_model(session_key) or DEFAULT_MODEL


def extract_model_prefix(message: str) -> tuple[str | None, str]:
    """Check if message starts with a model prefix like !opus or !s.

    Returns (model_name, cleaned_message). If no prefix, returns (None, original).
    Shortcuts (s, m, l, g, o, w) are expanded to their full alias.
    """
    m = _PREFIX_RE.match(message)
    if not m:
        return None, message
    tag = m.group(1).lower()
    model = _PI_SHORTCUTS.get(tag, tag)
    return model, message[m.end() :]


def list_available_models() -> list[tuple[str, str]]:
    """Run `pi --list-models` and return (model, details) pairs.

    Parses the tabular output from pi's model listing. Falls back to a
    hardcoded list of known aliases if pi is not available or the call
    fails.
    """
    try:
        result = subprocess.run(
            ["pi", "--list-models"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            models: list[tuple[str, str]] = []
            lines = result.stdout.strip().splitlines()
            # Skip header line: "provider  model   context  max-out  thinking  images"
            for line in lines[1:]:
                parts = line.split()
                if len(parts) >= 6:
                    provider = parts[0]
                    model_name = parts[1]
                    context = parts[2]
                    thinking = parts[4]
                    details = f"{provider}  ctx={context}  thinking={thinking}"
                    models.append((model_name, details))
            if models:
                return models
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Fallback: known pi litellm aliases with reasonable guesses
    return [
        ("small", "litellm  ctx=128K  thinking=yes"),
        ("medium", "litellm  ctx=128K  thinking=no"),
        ("large", "litellm  ctx=128K  thinking=yes"),
        ("gpt", "litellm  ctx=128K  thinking=yes"),
        ("opus", "litellm  ctx=200K  thinking=yes"),
        ("write", "litellm  ctx=200K  thinking=yes"),
        ("dsf", "litellm  ctx=128K  thinking=yes"),
        ("glm", "litellm  ctx=128K  thinking=no"),
    ]
